"""Numerical checks for phase, physical arrival time, and decay scaling."""
import copy
import json
import unittest
from pathlib import Path

import numpy as np

from simulate_room import fractional_octave_smooth, simulate, validate_config, waterfall


class AcousticsTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path(__file__).resolve().parents[1].joinpath("room_config.json").read_text())

    def test_geometry_and_nyquist_validation(self):
        cfg = copy.deepcopy(self.config)
        cfg["listener_m"][2] = cfg["room"]["dimensions_m"][2]
        with self.assertRaisesRegex(ValueError, "strictly inside"):
            validate_config(cfg)
        cfg = copy.deepcopy(self.config)
        cfg["plots"]["frequency_range_hz"][1] = 8000
        with self.assertRaisesRegex(ValueError, "Nyquist"):
            validate_config(cfg)
        cfg = copy.deepcopy(self.config)
        cfg["simulation"]["duration_s"] = 0.2
        with self.assertRaisesRegex(ValueError, "must cover"):
            validate_config(cfg)

    def test_flat_response_stays_flat_after_smoothing(self):
        freqs = np.arange(0, 8001, 2.0)
        np.testing.assert_allclose(fractional_octave_smooth(freqs, np.ones_like(freqs), 1 / 12), 1)

    def test_waterfall_preserves_decay_and_channel_gain(self):
        fs = 4000
        time = np.arange(4000) / fs
        rir = np.exp(-time / 0.1) * np.cos(2 * np.pi * 100 * time)
        plots = {**self.config["plots"], "waterfall_window_s": 0.2,
                 "waterfall_duration_s": 0.3, "waterfall_step_s": 0.01}
        freqs, times, levels = waterfall(np.vstack([rir, rir * 0.5]), fs, 0, plots)
        bin_index = np.argmin(np.abs(freqs - 100))
        np.testing.assert_allclose(levels[1, :, bin_index] - levels[0, :, bin_index],
                                   20 * np.log10(0.5), atol=1e-9)
        np.testing.assert_allclose(levels[0, :, bin_index] - levels[0, 0, bin_index],
                                   -20 / np.log(10) * times / 0.1, atol=1e-8)

    def test_direct_path_and_coherent_sum_keep_physical_delays(self):
        cfg = copy.deepcopy(self.config)
        cfg["room"]["absorption"] = 1.0
        cfg["simulation"]["max_order"] = 0
        cfg["speakers"][1]["position_m"] = [2.25, 2.8, 1.2]
        cfg["speakers"][1]["gain_db"] = -6.0
        cfg["speakers"][1]["delay_ms"] = 5.0
        cfg = validate_config(cfg)
        rirs, names, onset, info = simulate(cfg)
        fs = cfg["simulation"]["sample_rate_hz"]
        self.assertEqual(len(names), 3)
        for index, distance in enumerate(info["speaker_listener_distances_m"]):
            expected = distance / info["sound_speed_m_s"] * fs
            expected += round(cfg["speakers"][index]["delay_ms"] / 1000 * fs)
            self.assertLessEqual(abs(np.argmax(np.abs(rirs[index])) - expected), 1)
        np.testing.assert_allclose(rirs[-1], rirs[0] + rirs[1])
        self.assertNotEqual(np.argmax(abs(rirs[0])), np.argmax(abs(rirs[1])))
        self.assertLess(onset, np.argmax(abs(rirs[1])))


if __name__ == "__main__":
    unittest.main()
