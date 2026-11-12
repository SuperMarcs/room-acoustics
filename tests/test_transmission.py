"""Energy conservation, reciprocity, TL sensitivity, and complete GUI reports."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import numpy as np

from room_gui_transmission import PROJECT, RoomEditor, prepare_qt_plugins
from simulate_transmission import validate_config
from transmission_model import LINKS, exchange_matrices, solve_transmission


def example():
    cfg = json.loads((PROJECT / "room_config_transmission.json").read_text())
    cfg["simulation"]["n_rays"] = 1000
    cfg["simulation"]["max_order"] = 1
    return validate_config(cfg)


class TransmissionEnergyTests(unittest.TestCase):
    def test_exchange_conserves_energy_and_matches_absorption_loss(self):
        cfg = example()
        matrices, volumes, absorbed, _ = exchange_matrices(cfg)
        np.testing.assert_allclose(matrices.sum(axis=1), -343 / 4 * absorbed / volumes[None, :])
        source_power = sum(10 ** (speaker["gain_db"] / 10) for speaker in cfg["speakers"])
        result = solve_transmission(cfg)
        density = result["steady_density"].sum(axis=2)
        lost_power = 343 / 4 * (absorbed * density).sum(axis=1)
        np.testing.assert_allclose(lost_power, source_power, rtol=1e-12)
        self.assertTrue(np.all(np.linalg.eigvals(matrices).real < 0))

    def test_room_average_density_transfer_is_reciprocal(self):
        matrices, volumes, _, _ = exchange_matrices(example())
        for matrix in matrices:
            green = np.linalg.solve(-matrix, np.eye(3)) / volumes[:, None]
            np.testing.assert_allclose(green, green.T, atol=1e-14)

    def test_ten_db_more_loss_reduces_received_ratio_by_about_ten_db(self):
        cfg = example()
        cfg["transmission"]["loss_db"] = {key: None for key in LINKS}
        cfg["transmission"]["loss_db"]["wall_ab"] = 30
        first = solve_transmission(validate_config(cfg))
        cfg["transmission"]["loss_db"]["wall_ab"] = 40
        second = solve_transmission(validate_config(cfg))
        first_ratio = first["listener_total"][1] / first["listener_total"][0]
        second_ratio = second["listener_total"][1] / second["listener_total"][0]
        difference = 10 * np.log10(first_ratio / second_ratio)
        self.assertTrue(np.all((difference > 9.95) & (difference < 10.01)))

    def test_blocked_surfaces_isolate_and_open_door_restores_transfer(self):
        cfg = example()
        cfg["transmission"]["loss_db"] = {key: None for key in LINKS}
        blocked = solve_transmission(validate_config(cfg))
        np.testing.assert_array_equal(blocked["listener_total"][1:], 0)
        cfg["floor_plan"]["doors"]["room_a"]["open"] = True
        opened = solve_transmission(validate_config(cfg))
        self.assertTrue(np.all(opened["listener_total"][2] > 0))
        np.testing.assert_array_equal(opened["listener_total"][1], 0)
        np.testing.assert_array_equal(opened["summary"]["effective_tau"]["door_a"], 1)

    def test_switch_off_decays_and_same_room_listeners_share_estimate(self):
        cfg = example()
        cfg["listeners"][1]["position_m"] = [2.4, 4.3, 1.2]
        result = solve_transmission(validate_config(cfg))
        np.testing.assert_array_equal(result["listener_total"][0], result["listener_total"][1])
        self.assertTrue(np.all(np.diff(result["listener_decay"], axis=1) <= 1e-12))
        np.testing.assert_allclose(result["listener_decay"][:, 0], result["listener_total"])
        self.assertTrue(np.all(result["listener_decay"] >= 0))

    def test_invalid_loss_budget_and_non_decaying_model_are_rejected(self):
        for loss in (-1, 151, float("nan"), True):
            cfg = example()
            cfg["transmission"]["loss_db"]["wall_ab"] = loss
            with self.assertRaises(ValueError):
                validate_config(cfg)
        cfg = example()
        cfg["transmission"]["loss_db"]["door_b"] = 0
        with self.assertRaisesRegex(ValueError, "exceeds 1"):
            validate_config(cfg)
        cfg = example()
        cfg["materials"] = {key: 0 for key in cfg["materials"]}
        with self.assertRaisesRegex(ValueError, "needs absorption"):
            validate_config(cfg)

    def test_legacy_connected_configuration_loads_without_mutation(self):
        old = json.loads((PROJECT / "room_config_connected.json").read_text())
        before = copy.deepcopy(old)
        upgraded = validate_config(old)
        self.assertIn("transmission", upgraded)
        self.assertEqual(old, before)
        upgraded["transmission"]["enabled"] = False
        self.assertIsNone(solve_transmission(validate_config(upgraded)))


@unittest.skipUnless(os.environ.get("ROOM_GUI_TEST") == "1", "Set ROOM_GUI_TEST=1 for Qt checks")
class TransmissionDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        prepare_qt_plugins()
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="transmission-editor-")
        path = Path(self.temp.name) / "room.json"
        path.write_text(json.dumps(example()))
        self.window = RoomEditor(path)
        self.window.output_var.setText(self.temp.name)
        self.window.show()
        self.qt.processEvents()

    def tearDown(self):
        self.window.stop_worker()
        self.window.hide()
        self.window.deleteLater()
        self.qt.processEvents()
        self.temp.cleanup()

    def test_live_tl_edit_validation_and_blocked_entry(self):
        w = self.window
        w.tl_table.item(0, 0).setText("20")
        self.assertEqual(w.cfg["transmission"]["loss_db"]["door_a"][0], 20)
        w.tl_table.item(0, 0).setText("-1")
        self.assertFalse(w.create_button.isEnabled())
        w.tl_table.item(0, 0).setText("blocked")
        self.assertTrue(w.create_button.isEnabled())
        self.assertIsNone(w.cfg["transmission"]["loss_db"]["door_a"][0])

    def test_report_uses_click_snapshot_and_exports_leakage_separately(self):
        from unittest.mock import patch

        w = self.window
        with patch("room_gui_transmission.W.QMessageBox.warning") as warning:
            w.create_report()
            w.tl_table.item(0, 0).setText("24")
            deadline = time.monotonic() + 55
            while w.process is not None and time.monotonic() < deadline:
                self.qt.processEvents()
                time.sleep(0.03)
            self.assertIsNone(w.process, "Report worker timed out")
            self.assertFalse(warning.called, str(warning.call_args))
        self.assertTrue(w.last_report.exists())
        summary = json.loads((w.run_dir / "run_summary.json").read_text())
        self.assertEqual(summary["config"]["transmission"]["loss_db"]["door_a"][0], 15)
        self.assertTrue((w.run_dir / "transmission_comparison.png").exists())
        self.assertTrue((w.run_dir / "transmission_decay_listener_02.png").exists())
        self.assertTrue((w.run_dir / "transmission_levels.csv").exists())
        self.assertEqual(len(list(w.run_dir.glob("air_path_bass_waterfall_*.png"))), 1)
        self.assertEqual(summary["report_rendering"]["waterfall_pages"], 1)
        transmission = np.load(w.run_dir / "transmission_data.npz")
        baseline = np.load(w.run_dir / "air_path_simulation_data.npz")
        self.assertTrue(np.all(transmission["listener_total_density"][1] > 0))
        np.testing.assert_array_equal(baseline["rir"][1], 0)
        self.assertIn("Layout has changed", w.status_label.text())


if __name__ == "__main__":
    unittest.main()
