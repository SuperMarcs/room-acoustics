"""Door connectivity, exact boundary geometry, hybrid propagation, and editor checks."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import numpy as np

from connected_geometry import Geometry, SPACES, door_bounds, locate_space, resize_positions
from room_gui_connected import PROJECT, RoomEditor, prepare_qt_plugins
from simulate_connected_rooms import build_component_room, db, simulate, validate_config


def example():
    cfg = json.loads((PROJECT / "room_config_connected.json").read_text())
    cfg["simulation"]["n_rays"] = 4000
    return validate_config(cfg)


class ConnectedGeometryTests(unittest.TestCase):
    def test_door_states_change_connectivity_and_preserve_lintels(self):
        for a_open, b_open, expected in ((True, True, 1), (False, True, 2),
                                         (True, False, 2), (False, False, 3)):
            cfg = example()
            cfg["floor_plan"]["doors"]["room_a"]["open"] = a_open
            cfg["floor_plan"]["doors"]["room_b"]["open"] = b_open
            geom = Geometry(cfg["floor_plan"])
            self.assertEqual(geom.count, expected)
            plan = cfg["floor_plan"]
            x = sum(door_bounds(plan, "room_a")) / 2
            y = plan["corridor_depth_m"] + plan["wall_thickness_m"] / 2
            self.assertEqual(geom.component_at([x, y, 1.0]) > 0, a_open)
            self.assertEqual(geom.component_at([x, y, 2.4]), 0)

    def test_boundary_orientation_and_engine_volume(self):
        cfg = example()
        for opened in (True, False):
            cfg["floor_plan"]["doors"]["room_a"]["open"] = opened
            geometry = Geometry(cfg["floor_plan"])
            for component in range(1, geometry.count + 1):
                room = build_component_room(cfg, geometry, component)
                self.assertAlmostEqual(room.get_volume(), geometry.volume(component), places=4)
            if not opened:
                self.assertTrue(any(surface.kind == "door" for c in range(1, geometry.count + 1)
                                    for surface in geometry.surfaces(c)))

    def test_reject_solid_wall_positions_and_invalid_door(self):
        cfg = example()
        cfg["listeners"][0]["position_m"] = [4.575, 3.0, 1.2]
        with self.assertRaisesRegex(ValueError, "inside Room A"):
            validate_config(cfg)
        cfg = example()
        cfg["floor_plan"]["doors"]["room_b"]["width_m"] = 8
        with self.assertRaisesRegex(ValueError, "fit strictly"):
            validate_config(cfg)

    def test_resize_preserves_local_positions_in_room_b(self):
        cfg = example()
        plan = copy.deepcopy(cfg["floor_plan"])
        plan["room_a_width_m"] += 1.0
        plan["corridor_depth_m"] += 0.5
        updated = validate_config(resize_positions(cfg, plan))
        np.testing.assert_allclose(np.subtract(updated["listeners"][1]["position_m"], cfg["listeners"][1]["position_m"]),
                                   [1, 0.5, 0])

    def test_hybrid_reaches_remote_room_but_closed_door_blocks_it(self):
        cfg = example()
        opened, _, _, info = simulate(cfg)
        self.assertEqual(info["connected_air_spaces"], 1)
        self.assertGreater(np.max(abs(opened[1])), 1e-8)
        cfg["floor_plan"]["doors"]["room_b"]["open"] = False
        closed, _, _, info = simulate(validate_config(cfg))
        self.assertEqual(info["connected_air_spaces"], 2)
        np.testing.assert_array_equal(closed[1], 0)
        self.assertGreater(np.max(abs(closed[0])), 1e-8)
        self.assertTrue(any("Seat B" in note and "isolated" in note for note in info["notes"]))
        np.testing.assert_allclose(opened[:, -1], opened[:, :-1].sum(axis=1))

    def test_no_path_is_not_displayed_as_zero_db(self):
        np.testing.assert_array_equal(db(np.zeros(4), 0), -300)


@unittest.skipUnless(os.environ.get("ROOM_GUI_TEST") == "1", "Set ROOM_GUI_TEST=1 for Qt checks")
class ConnectedDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        prepare_qt_plugins()
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="connected-rooms-")
        path = Path(self.temp.name) / "layout.json"
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

    def test_live_doors_local_coordinates_and_drag_between_spaces(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        w = self.window
        w.select("listener:1")
        self.assertEqual(w.space_combo.currentText(), "Room B")
        self.assertAlmostEqual(float(w.pos_vars[0].text()), 2)
        w.pos_vars[0].setText("2.4")
        self.assertAlmostEqual(w.cfg["listeners"][1]["position_m"][0], 7.05)
        w.plan_vars["room_a_width_m"].setText("5.5")
        self.assertAlmostEqual(w.cfg["listeners"][1]["position_m"][0], 8.05)
        self.assertAlmostEqual(float(w.pos_vars[0].text()), 2.4)
        w.door_open["room_b"].setChecked(False)
        self.assertEqual(Geometry(w.cfg["floor_plan"]).count, 2)
        self.qt.processEvents()
        canvas = w.canvas
        start = canvas.marker_pixels["listener:1"].toPoint()
        end = canvas.to_pixel(2.0, 0.8).toPoint()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(canvas, end)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(locate_space(w.cfg["floor_plan"], w._position()), "corridor")
        self.assertEqual(w.space_combo.currentText(), "Corridor")
        w.add_listener()
        self.assertEqual(locate_space(w.cfg["floor_plan"], w._position()), "corridor")
        w.plan_vars["height_m"].setText("")
        self.assertFalse(w.create_button.isEnabled())

    def test_report_uses_door_snapshot_and_records_isolated_listener(self):
        from unittest.mock import patch

        w = self.window
        w.door_open["room_b"].setChecked(False)
        with patch("room_gui_connected.W.QMessageBox.warning") as warning:
            w.create_report()
            w.door_open["room_b"].setChecked(True)
            deadline = time.monotonic() + 55
            while w.process is not None and time.monotonic() < deadline:
                self.qt.processEvents()
                time.sleep(0.03)
            self.assertIsNone(w.process, "Report worker timed out")
            self.assertFalse(warning.called, str(warning.call_args))
        self.assertTrue(w.last_report.exists())
        info = json.loads((w.run_dir / "run_summary.json").read_text())
        self.assertFalse(info["config"]["floor_plan"]["doors"]["room_b"]["open"])
        self.assertEqual(info["connected_air_spaces"], 2)
        self.assertTrue((w.run_dir / "model_notes.png").exists())
        self.assertTrue((w.run_dir / "listener_comparison.png").exists())
        data = np.load(w.run_dir / "simulation_data.npz")
        np.testing.assert_array_equal(data["rir"][1], 0)
        self.assertTrue(np.isfinite(data["response_db"]).all())
        self.assertIn("Layout has changed", w.status_label.text())


if __name__ == "__main__":
    unittest.main()
