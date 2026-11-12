"""Speaker Studio → room transfer, portable configurations, and new GUI checks."""
import copy
import csv
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault("MPLBACKEND", "Agg")
import numpy as np

import simulate_room_designed_speakers as room
from speaker_profile import (validate_profile, profile_response, profile_id, attach_profile,
                             source_profile, combine_responses, load_profile)

PROJECT = Path(__file__).resolve().parents[1]


def profile(value=1.0, name="Test speaker", frequencies=None, pressure=None):
    f = np.array([10.0, 100.0, 1000.0, 20000.0]) if frequencies is None else np.asarray(frequencies)
    h = np.full(f.shape, complex(value)) if pressure is None else np.asarray(pressure, dtype=complex)
    return {"format": "speaker-studio-room-profile", "version": 1, "name": name,
            "reference_distance_m": 1.0, "reference_voltage_rms": 2.83,
            "sound_speed_m_s": 343.0, "propagation_delay_removed": True,
            "equivalent_omnidirectional": True, "frequency_hz": f.tolist(),
            "pressure_real_pa": h.real.tolist(), "pressure_imag_pa": h.imag.tolist()}


def config():
    cfg = json.loads((PROJECT / "room_config_designed_speakers.json").read_text())
    cfg["simulation"]["max_order"] = 0
    cfg["plots"]["smoothing_octaves"] = 0
    return room.validate_config(cfg)


class SpeakerBridgeTests(unittest.TestCase):
    def test_profile_validation_and_unsupported_band(self):
        p = validate_profile(profile())
        h = profile_response(p, [0, 5, 10, 100, 20000, 21000])
        self.assertTrue(np.isnan(h[[0, 1, 5]]).all())
        np.testing.assert_allclose(h[[2, 3, 4]], 1.0)
        for edit in (lambda p: p.update(propagation_delay_removed=False),
                     lambda p: p.update(reference_distance_m=2),
                     lambda p: p.update(format="random_csv"),
                     lambda p: p["frequency_hz"].__setitem__(1, 10),
                     lambda p: p["pressure_real_pa"].__setitem__(0, float("nan")),
                     lambda p: p["pressure_real_pa"].__setitem__(0, True),
                     lambda p: p["pressure_imag_pa"].pop()):
            p = profile(); edit(p)
            with self.assertRaises(ValueError):
                validate_profile(p)

    def test_shared_embedded_profile_round_trip_and_mutation_protection(self):
        original = config(); p = profile()
        first = attach_profile(original, 0, p)
        both = room.validate_config(attach_profile(first, 1, p))
        self.assertEqual(len(both["speaker_profiles"]), 1)
        self.assertFalse(original["speaker_profiles"])
        p["pressure_real_pa"][0] = 99
        self.assertEqual(source_profile(both, 0)["pressure_real_pa"][0], 1)
        reopened = room.validate_config(json.loads(json.dumps(both)))
        self.assertEqual(reopened, both)
        cleared = room.validate_config(attach_profile(both, 0, None))
        self.assertEqual(len(cleared["speaker_profiles"]), 1)
        cleared = room.validate_config(attach_profile(cleared, 1, None))
        self.assertFalse(cleared["speaker_profiles"])
        tampered = copy.deepcopy(both)
        source_profile(tampered, 0)["pressure_real_pa"][0] = 2
        with self.assertRaisesRegex(ValueError, "changed"):
            room.validate_config(tampered)
        missing = copy.deepcopy(both); missing["speaker_profiles"] = {}
        with self.assertRaisesRegex(ValueError, "missing"):
            room.validate_config(missing)

    def test_each_profile_is_applied_before_coherent_summation(self):
        cfg = attach_profile(attach_profile(config(), 0, profile(1)), 1, profile(-0.5))
        cfg = room.validate_config(cfg)
        f = np.array([20, 100, 1000])
        raw = np.ones((2, 3, 3), dtype=complex)
        raw[:, 1] *= 2; raw[:, 2] *= 3
        result, sources = combine_responses(cfg, raw, f)
        np.testing.assert_allclose(result[:, 0], 4*np.pi)
        np.testing.assert_allclose(result[:, 1], -4*np.pi)
        np.testing.assert_allclose(result[:, -1], 0, atol=1e-12)
        np.testing.assert_allclose(result[:, -1], result[:, :-1].sum(axis=1))

    def test_complex_delay_and_distance_are_not_applied_twice(self):
        f = np.arange(10., 8001.)
        p = profile(frequencies=f, pressure=0.5*np.exp(-2j*np.pi*f*0.025))
        targets = np.array([100.25, 200.5, 1000.75])
        expected = 0.5*np.exp(-2j*np.pi*targets*0.025)
        np.testing.assert_allclose(profile_response(p, targets), expected, rtol=2e-6)
        cfg = config(); cfg["speakers"] = cfg["speakers"][:1]
        cfg = room.validate_config(attach_profile(cfg, 0, p))
        distance = 3.0
        raw = (np.exp(-2j*np.pi*targets*distance/343)/(4*np.pi*distance))[None, None]
        out, _ = combine_responses(cfg, raw, targets)
        np.testing.assert_allclose(out[0, 0], expected*np.exp(-2j*np.pi*targets*distance/343)/distance,
                                   rtol=2e-6)

    def test_profile_level_and_phase_survive_real_room_simulation(self):
        cfg = room.validate_config(attach_profile(config(), 0, profile(2)))
        raw, names, onset, info = room.simulate(cfg)
        data = room.calculate_responses(cfg, raw)
        valid = (data["frequency"] >= 20) & (data["frequency"] <= 7000)
        np.testing.assert_allclose(data["pressure"][:, 0, valid],
                                   data["room_transfer"][:, 0, valid]*8*np.pi)
        np.testing.assert_allclose(data["pressure"][:, 1, valid],
                                   data["room_transfer"][:, 1, valid]*4*np.pi)
        np.testing.assert_allclose(data["pressure"][:, -1, valid], data["pressure"][:, :-1, valid].sum(axis=1))
        self.assertEqual(len(names), 3)

    def test_limited_measured_band_and_smoothing_are_finite_only_in_band(self):
        cfg = config(); cfg["plots"]["smoothing_octaves"] = 1/12
        p = profile(frequencies=[100, 200, 500, 2000])
        cfg = room.validate_config(attach_profile(cfg, 0, p))
        raw, _, _, _ = room.simulate(cfg)
        data = room.calculate_responses(cfg, raw)
        supported = (data["frequency"] >= 100) & (data["frequency"] <= 2000)
        self.assertTrue(np.isfinite(data["relative_db"][:, -1, supported]).all())
        self.assertTrue(np.isnan(data["relative_db"][:, -1, ~supported]).all())

    def test_report_has_compact_graphs_and_separate_raw_room_data(self):
        cfg = room.validate_config(attach_profile(config(), 0, profile()))
        raw, names, onset, info = room.simulate(cfg)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            room.export_report(cfg, out, raw, names, onset, info)
            self.assertTrue((out / "acoustics_report.pdf").exists())
            self.assertLess((out / "acoustics_report.pdf").stat().st_size, 1_000_000)
            self.assertTrue((out / "speaker_design_responses.png").exists())
            self.assertTrue((out / "frequency_response_listener_02.png").exists())
            self.assertFalse(list(out.glob("*waterfall*")))
            summary = json.loads((out / "run_summary.json").read_text())
            self.assertEqual(summary["report_rendering"]["waterfall_pages"], 0)
            self.assertEqual(len(summary["config"]["speaker_profiles"]), 1)
            saved = room.validate_config(json.loads((out / "room_config.json").read_text()))
            self.assertEqual(saved, cfg)
            with np.load(out / "simulation_data.npz") as data:
                np.testing.assert_allclose(data["room_rir"], raw)
                self.assertEqual(data["transfer_function"].shape[:2], (2, 3))
                self.assertNotIn("rir", data.files)


@unittest.skipUnless(os.environ.get("SPEAKER_ROOM_EXPORT"), "Set SPEAKER_ROOM_EXPORT to check a Julia export")
class JuliaExportTests(unittest.TestCase):
    def test_export_matches_reference_graph_and_removes_only_reference_flight(self):
        path = Path(os.environ["SPEAKER_ROOM_EXPORT"])
        profile = load_profile(path)
        with (path.parent / "response.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        freq = np.asarray([float(r["frequency_Hz"]) for r in rows])
        expected = 20e-6 * 10**(np.asarray([float(r["total_SPL_dB"]) for r in rows])/20)
        expected = expected*np.exp(1j*np.deg2rad([float(r["total_phase_deg"]) for r in rows]))
        actual = profile_response(profile, freq)*np.exp(-2j*np.pi*freq/343)
        np.testing.assert_allclose(actual, expected, rtol=2e-4, atol=1e-8)
        self.assertEqual(profile["reference_distance_m"], 1)
        self.assertTrue(profile["propagation_delay_removed"])


@unittest.skipUnless(os.environ.get("ROOM_GUI_TEST") == "1", "Set ROOM_GUI_TEST=1 for Qt checks")
class DesignedSpeakerGUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from room_gui_designed_speakers import prepare_qt_plugins
        prepare_qt_plugins()
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        from room_gui_designed_speakers import RoomEditor
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        cfg_path = self.folder / "room.json"; cfg_path.write_text(json.dumps(config()))
        self.profile_path = self.folder / "speaker_room_profile.json"
        exported = os.environ.get("SPEAKER_ROOM_EXPORT")
        self.profile_path.write_text(Path(exported).read_text() if exported else json.dumps(profile()))
        self.window = RoomEditor(cfg_path)
        self.window.output_var.setText(str(self.folder))
        self.window.show(); self.qt.processEvents()

    def tearDown(self):
        if self.window._file_dialog is not None:
            self.window._file_dialog.reject()
        self.window.stop_worker(); self.window.hide(); self.window.deleteLater()
        self.qt.processEvents(); self.temp.cleanup()

    def test_qt_file_pickers_accept_mouse_clicks_without_blocking_event_loop(self):
        from PySide6 import QtCore as C, QtWidgets as W
        from PySide6.QtTest import QTest

        w = self.window

        def choose(dialog, path, button_kind):
            self.assertIs(dialog, w._file_dialog)
            self.assertTrue(dialog.testOption(W.QFileDialog.Option.DontUseNativeDialog))
            self.assertEqual(dialog.windowModality(), C.Qt.WindowModality.WindowModal)
            dialog.setDirectory(str(path.parent))
            QTest.qWait(150)
            if button_kind==W.QDialogButtonBox.StandardButton.Save:
                field = dialog.findChild(W.QLineEdit, "fileNameEdit")
                self.assertIsNotNone(field)
                QTest.mouseClick(field, C.Qt.MouseButton.LeftButton)
                field.selectAll()
                QTest.keyClicks(field, path.name)
                self.assertEqual(field.text(), path.name)
            else:
                # Select a visible file/folder row with an actual mouse click.
                view=dialog.findChild(W.QTreeView, "treeView")
                self.assertIsNotNone(view)
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    index=view.model().index(str(path))
                    view.scrollTo(index)
                    self.qt.processEvents()
                    rect=view.visualRect(index)
                    if index.isValid() and not rect.isEmpty():
                        break
                    QTest.qWait(20)
                self.assertTrue(index.isValid())
                self.assertFalse(rect.isEmpty())
                QTest.mouseClick(view.viewport(), C.Qt.MouseButton.LeftButton, pos=rect.center())
            self.qt.processEvents()
            buttons = dialog.findChild(W.QDialogButtonBox)
            button = buttons.button(button_kind)
            self.assertIsNotNone(button)
            self.assertTrue(button.isEnabled())
            QTest.mouseClick(button, C.Qt.MouseButton.LeftButton)
            deadline = time.monotonic()+2
            while w._file_dialog is not None and time.monotonic()<deadline:
                QTest.qWait(20)
            self.assertIsNone(w._file_dialog)

        ticks = []
        heartbeat = C.QTimer()
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start()
        try:
            dialog = w.load_speaker_design() # Must return without entering exec().
            deadline = time.monotonic()+2
            while len(ticks)<2 and time.monotonic()<deadline:
                QTest.qWait(20) # Allow first-use filesystem/model initialization.
            self.assertGreater(len(ticks), 1)
            choose(dialog, self.profile_path, W.QDialogButtonBox.StandardButton.Open)
        finally:
            heartbeat.stop()
        identifier = w.cfg["speakers"][0]["profile_id"]

        saved_path = self.folder / "saved_room.json"
        choose(w.save_config(), saved_path, W.QDialogButtonBox.StandardButton.Save)
        self.assertTrue(saved_path.is_file())
        self.assertEqual(json.loads(saved_path.read_text())["speakers"][0]["profile_id"], identifier)

        reopened = json.loads(saved_path.read_text())
        reopened["room"]["dimensions_m"][2] = 3.0
        saved_path.write_text(json.dumps(reopened))
        choose(w.load_config(), saved_path, W.QDialogButtonBox.StandardButton.Open)
        self.assertEqual(w.cfg["room"]["dimensions_m"][2], 3.0)

        reports = self.folder / "chosen_reports"
        reports.mkdir()
        choose(w.browse_output(), reports, W.QDialogButtonBox.StandardButton.Open)
        self.assertEqual(Path(w.output_var.text()), reports)

        revision = w.revision
        dialog = w.load_speaker_design()
        self.assertIs(w.load_speaker_design(), dialog) # At most one picker per editor.
        QTest.qWait(80)
        cancel = dialog.findChild(W.QDialogButtonBox).button(W.QDialogButtonBox.StandardButton.Cancel)
        QTest.mouseClick(cancel, C.Qt.MouseButton.LeftButton)
        self.qt.processEvents()
        self.assertIsNone(w._file_dialog)
        self.assertEqual(w.revision, revision)
        self.assertEqual(w.cfg["speakers"][0]["profile_id"], identifier)

    def test_pasted_path_import_does_not_open_a_dialog(self):
        from PySide6 import QtCore as C
        from PySide6.QtTest import QTest
        w=self.window
        w.profile_path_var.setText('"'+str(self.profile_path)+'"')
        QTest.mouseClick(w.load_profile_path_button, C.Qt.MouseButton.LeftButton)
        self.qt.processEvents()
        self.assertIsNone(w._file_dialog)
        self.assertIn("profile_id",w.cfg["speakers"][0])
        self.assertEqual(w.preview_tabs.currentIndex(),1)
        w.select("listener:0")
        self.assertFalse(w.load_profile_path_button.isEnabled())

    def test_assign_select_clear_save_graph_and_run_frozen_report(self):
        w = self.window
        w.apply_speaker_profile(self.profile_path)
        first = w.cfg["speakers"][0]["profile_id"]
        self.assertEqual(w.preview_tabs.currentIndex(), 1)
        w.select("speaker:1"); w.apply_speaker_profile(self.profile_path)
        self.assertEqual(w.cfg["speakers"][1]["profile_id"], first)
        self.assertEqual(len(w.cfg["speaker_profiles"]), 1)
        w.select("listener:0")
        self.assertFalse(w.load_profile_button.isEnabled())
        w.select("speaker:0")
        w.create_report()
        w.clear_speaker_design() # The in-progress report must retain its captured response.
        deadline = time.monotonic()+50
        while w.process is not None and time.monotonic()<deadline:
            self.qt.processEvents(); time.sleep(0.03)
        self.assertIsNone(w.process)
        self.assertIsNotNone(w.last_report)
        captured = json.loads((w.run_dir / "run_summary.json").read_text())
        self.assertEqual(captured["config"]["speakers"][0]["profile_id"], first)
        self.assertNotIn("profile_id", w.cfg["speakers"][0])
        self.assertIn("changed", w.status_label.text())
        w.select("speaker:1"); w.preview_tabs.setCurrentIndex(1); self.qt.processEvents()
        image_path = os.environ.get("SPEAKER_ROOM_PREVIEW")
        if image_path:
            w.grab().save(image_path)


if __name__ == "__main__":
    unittest.main()
