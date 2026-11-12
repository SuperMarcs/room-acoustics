"""Multiple receiver timing, level comparisons, and interactive editing checks."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import numpy as np

import simulate_room_multi as multi
from room_gui_multi import PROJECT, RoomEditor, prepare_qt_plugins, resize_room
from simulate_room import simulate as simulate_single, validate_config as validate_single


def example():
    cfg = json.loads((PROJECT / "room_config_multi.json").read_text())
    cfg["simulation"]["max_order"] = 0
    return cfg


class MultipleReceiverTests(unittest.TestCase):
    def test_validation_and_legacy_import(self):
        old = json.loads((PROJECT / "room_config.json").read_text())
        imported = multi.validate_config(old)
        self.assertEqual(len(imported["listeners"]), 1)
        self.assertNotIn("listener_m", imported)
        self.assertIn("listener_m", old)
        for modify in (
            lambda cfg: cfg.update(listeners=[]),
            lambda cfg: cfg["listeners"][1].update(name="Listener 1"),
            lambda cfg: cfg["listeners"][1].update(position_m=[0, 2, 1.2]),
            lambda cfg: cfg["listeners"][1].update(position_m=cfg["speakers"][0]["position_m"]),
        ):
            cfg = example()
            modify(cfg)
            with self.assertRaises(ValueError):
                multi.validate_config(cfg)

    def test_receiver_timing_and_sum_agree_with_independent_simulations(self):
        cfg = multi.validate_config(example())
        rirs, names, onset, info = multi.simulate(cfg)
        self.assertEqual(rirs.shape[:2], (2, 3))
        self.assertEqual(len(names), 3)
        np.testing.assert_allclose(rirs[:, -1], rirs[:, :-1].sum(axis=1))
        fs = cfg["simulation"]["sample_rate_hz"]
        for listener_index, listener in enumerate(cfg["listeners"]):
            independent = copy.deepcopy(cfg)
            independent.pop("listeners")
            independent["listener_m"] = listener["position_m"]
            single, _, _, _ = simulate_single(validate_single(independent))
            count = min(single.shape[-1], rirs.shape[-1])
            np.testing.assert_allclose(rirs[listener_index, :, :count], single[:, :count])
            for source_index, distance in enumerate(info["speaker_listener_distances_m"][listener_index]):
                expected = distance / info["sound_speed_m_s"] * fs
                self.assertLessEqual(abs(np.argmax(abs(rirs[listener_index, source_index])) - expected), 1)

    def test_resize_clamps_every_listener_without_mutating_original(self):
        cfg = multi.validate_config(example())
        before = copy.deepcopy(cfg)
        resized = resize_room(cfg, [3, 4, 1.4])
        self.assertEqual(resized["listeners"][1]["position_m"], [2.99, 3.99, 1.2])
        multi.validate_config(resized)
        self.assertEqual(cfg, before)


@unittest.skipUnless(os.environ.get("ROOM_GUI_TEST") == "1", "Set ROOM_GUI_TEST=1 for Qt checks")
class MultipleListenerDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        prepare_qt_plugins()
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multi-listener-")
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

    def test_add_drag_rename_remove_and_protect_last_listener(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window = self.window
        window.add_listener()
        self.assertEqual(window.selected, "listener:2")
        window.name_var.setText("Rear seat")
        window.pos_vars[2].setText("1.4")
        self.assertEqual(window.cfg["listeners"][2]["name"], "Rear seat")
        self.assertEqual(window.cfg["listeners"][2]["position_m"][2], 1.4)
        self.qt.processEvents()
        plan = window.canvas
        start = plan.marker_pixels["listener:2"].toPoint()
        end = plan.to_pixel(1.6, 4.8).toPoint()
        QTest.mousePress(plan, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(plan, end)
        QTest.mouseRelease(plan, Qt.MouseButton.LeftButton, pos=end)
        self.assertAlmostEqual(window.cfg["listeners"][2]["position_m"][0], 1.6, delta=0.03)
        self.assertAlmostEqual(window.cfg["listeners"][2]["position_m"][1], 4.8, delta=0.03)
        self.assertEqual(len(window.cfg["speakers"]), 2)
        window.remove_object()
        window.remove_object()
        self.assertEqual(len(window.cfg["listeners"]), 1)
        self.assertFalse(window.remove_button.isEnabled())
        window.remove_object()
        self.assertEqual(len(window.cfg["listeners"]), 1)

    def test_report_captures_all_listeners_and_keeps_comparison_levels(self):
        window = self.window
        window.create_report()
        window.select("listener:1")
        window.pos_vars[0].setText("3.3")
        deadline = time.monotonic() + 55
        while window.process is not None and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(0.03)
        self.assertIsNone(window.process, "Report worker timed out")
        self.assertIsNotNone(window.last_report)
        summary = json.loads((window.run_dir / "run_summary.json").read_text())
        self.assertEqual(summary["config"]["listeners"][1]["position_m"][0], 3.1)
        self.assertIn("Layout has changed", window.status_label.text())
        self.assertTrue((window.run_dir / "listener_comparison.png").exists())
        self.assertTrue((window.run_dir / "frequency_response_listener_02.png").exists())
        self.assertTrue((window.run_dir / "bass_waterfall_listener_02_channel_03.png").exists())
        self.assertEqual(len(list(window.run_dir.glob("bass_waterfall_*.png"))), 2)
        self.assertFalse((window.run_dir / "bass_waterfall_listener_01_channel_01.png").exists())
        self.assertEqual(summary["report_rendering"]["waterfall_pages"], 2)
        self.assertIn(b"/Subtype /Image", window.last_report.read_bytes())
        data = np.load(window.run_dir / "simulation_data.npz")
        self.assertEqual(data["rir"].shape[:2], (2, 3))
        self.assertEqual(data["waterfall_db"].shape[:2], (2, 3))
        # A common reference preserves the response ratio between positions.
        from simulate_room_multi import fractional_octave_smooth

        freq = data["frequency_hz"]
        index = np.argmin(abs(freq - 100))
        levels = data["response_db"][:, -1, index]
        magnitudes = [fractional_octave_smooth(freq, abs(h), summary["config"]["plots"]["smoothing_octaves"])[index]
                      for h in data["transfer_function"][:, -1]]
        self.assertAlmostEqual(levels[0] - levels[1], 20 * np.log10(magnitudes[0] / magnitudes[1]))


if __name__ == "__main__":
    unittest.main()
