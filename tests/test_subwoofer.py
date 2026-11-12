"""Modal acoustics, constrained search and actual Play/slider/report workflows."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import numpy as np

from subwoofer_model import (PROJECT, ModalRoom, calculate, fingerprint, level_db,
                             optimize, validate_config)


def example():
    return validate_config(json.loads((PROJECT / "subwoofer_config.json").read_text()))


class ModalBassTests(unittest.TestCase):
    def test_axial_mode_frequency_and_center_node(self):
        cfg = example()
        model = ModalRoom(cfg)
        index = np.flatnonzero(np.all(model.indices == [1, 0, 0], axis=1))[0]
        self.assertAlmostEqual(model.mode_frequencies[index], 343 / (2 * cfg["room"]["dimensions_m"][0]))
        value = model.basis([[cfg["room"]["dimensions_m"][0] / 2, 1, 1]])[0, index]
        self.assertAlmostEqual(value, 0, places=14)

    def test_reciprocity_and_complex_polarity_cancellation(self):
        cfg = example()
        model = ModalRoom(cfg)
        source = copy.deepcopy(cfg["subwoofers"][0])
        receiver = cfg["listeners"][0]["position_m"]
        forward = model.pressure([receiver], [source])
        reversed_source = copy.deepcopy(source)
        reversed_source["position_m"] = receiver
        reverse = model.pressure([source["position_m"]], [reversed_source])
        np.testing.assert_allclose(forward, reverse, atol=1e-12)
        negative = copy.deepcopy(source)
        negative["polarity"] = -1
        np.testing.assert_allclose(model.pressure([receiver], [source, negative]), 0, atol=1e-14)
        delayed = copy.deepcopy(source)
        delayed["delay_ms"] = 5
        expected = forward * np.exp(-2j * np.pi * model.frequencies * 0.005)
        np.testing.assert_allclose(model.pressure([receiver], [delayed]), expected, atol=1e-12)

    def test_level_reference_is_fixed_and_gain_is_in_db(self):
        cfg = example()
        model = ModalRoom(cfg)
        sub = copy.deepcopy(cfg["subwoofers"][0])
        seats = [seat["position_m"] for seat in cfg["listeners"]]
        original = model.pressure(seats, [sub])
        sub["gain_db"] = 6
        changed = model.pressure(seats, [sub])
        np.testing.assert_allclose(level_db(changed) - level_db(original), 6, atol=1e-10)

    def test_cached_slice_agrees_with_direct_frequency_solve_and_masks_near_source(self):
        cfg = example()
        cfg["map"]["height_m"] = 0.3
        result = calculate(cfg)
        index = int(np.flatnonzero(result["frequencies_hz"] == 63)[0])
        x_index, y_index = 15, 20
        point = [result["x_m"][x_index], result["y_m"][y_index], 0.3]
        direct = level_db(ModalRoom(cfg, [63]).pressure([point]))[0, 0]
        self.assertAlmostEqual(float(result["map_db"][index, y_index, x_index]), direct, places=4)
        self.assertTrue(np.isnan(result["map_db"]).any())
        self.assertEqual(str(result["fingerprint"]), fingerprint(cfg))

    def test_search_preserves_locked_subs_and_does_not_worsen_objective(self):
        cfg = example()
        cfg["subwoofers"][0]["locked"] = True
        original = copy.deepcopy(cfg)
        result = optimize(cfg)
        self.assertEqual(cfg, original)
        self.assertEqual(result["best_config"]["subwoofers"][0], cfg["subwoofers"][0])
        self.assertLessEqual(result["best_metrics"]["score_db"], result["baseline_metrics"]["score_db"] + 1e-10)
        self.assertGreaterEqual(result["best_metrics"]["mean_level_db"], result["minimum_mean_level_db"] - 1e-10)
        validate_config(result["best_config"])

    def test_import_old_rectangular_configuration_and_reject_invalid_inputs(self):
        old = json.loads((PROJECT / "room_config_multi.json").read_text())
        new = validate_config(old)
        self.assertEqual(len(new["subwoofers"]), len(old["speakers"]))
        self.assertNotIn("absorption", new["room"])
        invalid = example()
        invalid["subwoofers"][0]["position_m"][0] = -1
        with self.assertRaises(ValueError):
            validate_config(invalid)
        invalid = example()
        invalid["model"]["frequency_range_hz"] = [100, 100]
        with self.assertRaises(ValueError):
            validate_config(invalid)


@unittest.skipUnless(os.environ.get("ROOM_GUI_TEST") == "1", "Set ROOM_GUI_TEST=1 for Qt checks")
class SubwooferDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from subwoofer_gui import W, prepare_qt_plugins

        prepare_qt_plugins()
        cls.qt = W.QApplication.instance() or W.QApplication([])

    def setUp(self):
        from subwoofer_gui import SubwooferEditor

        self.temp = tempfile.TemporaryDirectory(prefix="subwoofer-studio-")
        cfg = example()
        cfg["model"]["frequency_range_hz"] = [20, 120]
        cfg["map"]["grid_spacing_m"] = 0.25
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps(cfg))
        self.window = SubwooferEditor(path)
        self.window.output_field.setText(self.temp.name)
        self.window.show()
        self.qt.processEvents()

    def tearDown(self):
        self.window.playing = False
        self.window.stop_worker()
        self.window.hide()
        self.window.deleteLater()
        self.qt.processEvents()
        self.temp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if self.window.process is None and not self.window.debounce.isActive():
                return
            time.sleep(0.015)
        self.fail("Background job did not become idle")

    def test_play_slider_cache_and_live_drag_update(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        w = self.window
        w.toggle_play()
        self.wait_idle()
        self.assertIsNotNone(w.data)
        starts = w.worker_starts
        w.frequency_slider.setValue(63)
        self.qt.processEvents()
        self.assertEqual(w.worker_starts, starts)
        self.assertEqual(w.frequency_spin.value(), 63)
        self.assertEqual(w.data["frequencies_hz"][w.canvas.frequency_index], 63)
        self.assertFalse(w.canvas.image.isNull())
        start = w.canvas.markers["sub:0"].toPoint()
        end = w.canvas.to_pixel(0.8, 1.0).toPoint()
        QTest.mousePress(w.canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(w.canvas, end)
        QTest.mouseRelease(w.canvas, Qt.MouseButton.LeftButton, pos=end)
        self.wait_idle()
        self.assertGreater(w.worker_starts, starts)
        self.assertEqual(str(w.data["fingerprint"]), fingerprint(w.cfg))
        self.assertAlmostEqual(w.cfg["subwoofers"][0]["position_m"][0], 0.8, delta=0.05)

    def test_stale_result_is_discarded_and_invalid_entries_block_jobs(self):
        w = self.window
        w.toggle_play()
        w.position_fields[0].setText("0.9")  # Change before the first result can be accepted.
        self.wait_idle()
        self.assertEqual(str(w.data["fingerprint"]), fingerprint(w.cfg))
        self.assertGreaterEqual(w.worker_starts, 2)
        w.dim_fields[0].setText("")
        self.assertFalse(w.report_button.isEnabled())
        self.assertFalse(w.form_valid)
        w.dim_fields[0].setText("4.5")
        self.assertTrue(w.form_valid)

    def test_invalid_input_during_first_play_recovers_automatically(self):
        w = self.window
        w.toggle_play()
        w.dim_fields[0].setText("")
        self.wait_idle()
        self.assertIsNone(w.data)
        self.assertTrue(w.playing)
        w.dim_fields[0].setText("4.5")
        self.wait_idle()
        self.assertIsNotNone(w.data)
        self.assertEqual(str(w.data["fingerprint"]), fingerprint(w.cfg))

    def test_optimize_report_snapshot_and_undo(self):
        from unittest.mock import patch

        w = self.window
        original = copy.deepcopy(w.cfg)
        with patch("subwoofer_gui.W.QMessageBox.warning") as warning:
            w.start_job("optimize")
            self.wait_idle()
            self.assertFalse(warning.called, str(warning.call_args))
            self.assertIsNotNone(w.optimization)
            self.assertTrue(w.undo_button.isEnabled())
            self.assertLessEqual(w.optimization["best_metrics"]["score_db"], w.optimization["baseline_metrics"]["score_db"])
            snapshot = copy.deepcopy(w.cfg)
            w.frequency_spin.setValue(63)
            w.start_job("report")
            # A layout edit during report generation must not alter its saved snapshot.
            w.position_fields[0].setText("0.55")
            self.wait_idle()
            self.assertTrue(w.last_report.exists())
            summary = json.loads(w.last_report.with_name("run_summary.json").read_text())
            self.assertEqual(summary["config"], snapshot)
            self.assertEqual(summary["selected_frequency_hz"], 63)
            self.assertTrue(w.last_report.with_name("map_data.npz").exists())
            self.assertLess(w.last_report.stat().st_size, 600000)
            self.assertFalse(warning.called, str(warning.call_args))
            # Re-run search, then verify its undo restores exactly the prior layout.
            previous = copy.deepcopy(w.cfg)
            w.start_job("optimize")
            self.wait_idle()
            w.undo_optimization()
            self.wait_idle()
            self.assertEqual(w.cfg, previous)
            self.assertNotEqual(w.cfg, original)


if __name__ == "__main__":
    unittest.main()
