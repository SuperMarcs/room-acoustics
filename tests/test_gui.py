"""Geometry checks and optional real-window/report integration checks.

Set ROOM_GUI_TEST=1 to exercise Qt windows on a desktop session.
"""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from room_gui import PROJECT, RoomEditor, clamp_position, prepare_qt_plugins, resize_room
from simulate_room import validate_config


class GeometryTests(unittest.TestCase):
    def test_resize_keeps_interior_positions_and_clamps_exterior_in_3d(self):
        original = json.loads((PROJECT / "room_config.json").read_text())
        untouched = copy.deepcopy(original)
        shrunk = resize_room(original, [3, 4, 1.5])
        self.assertEqual(shrunk["speakers"][0]["position_m"], [1, 1, 1.2])
        self.assertEqual(shrunk["speakers"][1]["position_m"], [2.99, 1, 1.2])
        self.assertEqual(original, untouched)
        validate_config(shrunk)
        position = clamp_position([-1, 20, 8], [3, 4, 1.5])
        self.assertEqual(position, [0.01, 3.99, 1.49])


@unittest.skipUnless(os.environ.get("ROOM_GUI_TEST") == "1", "Needs a desktop; set ROOM_GUI_TEST=1")
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        prepare_qt_plugins()
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="room-gui-")
        config = json.loads((PROJECT / "room_config.json").read_text())
        config["simulation"]["max_order"] = 0  # Fast report exercising the entire export path.
        path = Path(self.temp.name) / "room.json"
        path.write_text(json.dumps(config))
        self.app = RoomEditor(path)
        self.app.output_var.setText(self.temp.name)
        self.app.show()
        self.qt.processEvents()

    def tearDown(self):
        self.app.stop_worker()
        self.app.hide()
        self.app.deleteLater()
        self.qt.processEvents()
        self.temp.cleanup()

    def test_live_edits_dragging_and_invalid_field_recovery(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.app.dim_vars[0].setText("5")
        self.qt.processEvents()
        self.assertEqual(self.app.cfg["room"]["dimensions_m"][0], 5)
        self.app.select("listener")
        self.qt.processEvents()
        canvas = self.app.canvas
        start = canvas.marker_pixels["listener"].toPoint()
        end = canvas.to_pixel(2.7, 4.2).toPoint()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
        self.qt.processEvents()
        # Pixel rounding limits the accuracy of a mouse drag.
        self.assertAlmostEqual(self.app.cfg["listener_m"][0], 2.7, delta=0.03)
        self.assertAlmostEqual(self.app.cfg["listener_m"][1], 4.2, delta=0.03)
        self.app.pos_vars[2].setText("1.4")
        self.assertEqual(self.app.cfg["listener_m"][2], 1.4)
        self.app.dim_vars[0].setText("")
        self.assertFalse(self.app.form_valid)
        self.assertFalse(self.app.create_button.isEnabled())
        self.app.dim_vars[0].setText("5")
        self.assertTrue(self.app.form_valid)
        self.app.add_speaker()
        self.assertEqual(len(self.app.cfg["speakers"]), 3)
        self.app.remove_speaker()
        self.assertEqual(len(self.app.cfg["speakers"]), 2)

    def test_background_report_uses_click_time_snapshot(self):
        self.app.create_report()
        self.assertIsNotNone(self.app.process)
        # A live edit while the worker runs must not mutate the report's snapshot.
        self.app.pos_vars[0].setText("1.3")
        self.assertEqual(self.app.cfg["speakers"][0]["position_m"][0], 1.3)
        deadline = time.monotonic() + 55
        while self.app.process is not None and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(0.03)
        self.assertIsNone(self.app.process, "Report worker timed out")
        self.assertIsNotNone(self.app.last_report)
        self.assertTrue(self.app.last_report.exists())
        summary = json.loads((self.app.run_dir / "run_summary.json").read_text())
        self.assertEqual(summary["config"]["speakers"][0]["position_m"][0], 1.0)
        self.assertIn("Layout has changed", self.app.status_label.text())
        self.assertTrue((self.app.run_dir / "frequency_response.png").exists())


if __name__ == "__main__":
    unittest.main()
