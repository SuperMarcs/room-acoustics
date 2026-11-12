"""Display limits preserve response extrema and omit redundant/silent pages."""
import unittest

import numpy as np

from report_rendering import (decay_listener_indices, display_curve, display_waterfall,
                              report_options, waterfall_indices)


class ReportRenderingTests(unittest.TestCase):
    def test_reduction_preserves_narrow_peaks_dips_and_does_not_change_input(self):
        frequencies = np.geomspace(20, 20000, 100000)
        values = np.sin(np.log(frequencies))
        values[12345], values[56789] = 12, -64
        before = values.copy()
        x, y = display_curve(frequencies, values, [20, 20000], 1200)
        self.assertLessEqual(len(x), 1200)
        self.assertEqual(y.max(), 12)
        self.assertEqual(y.min(), -64)
        self.assertEqual(x[0], frequencies[0])
        self.assertEqual(x[-1], frequencies[-1])
        self.assertTrue(np.all(np.diff(x) > 0))
        np.testing.assert_array_equal(values, before)

    def test_curve_only_contains_visible_range(self):
        frequencies = np.arange(10000)
        x, y = display_curve(frequencies, np.cos(frequencies), [20, 300], 1200)
        np.testing.assert_array_equal(x, frequencies[20:301])
        self.assertEqual(len(y), len(x))

    def test_combined_pages_are_limited_and_silent_channels_are_omitted(self):
        rirs = np.ones((15, 100))  # 5 listeners, 2 speakers + sum
        rirs[5] = 0
        options = report_options({})
        self.assertEqual(waterfall_indices(rirs, 3, options), [2, 8, 11])
        options["report_waterfalls"] = "none"
        self.assertEqual(waterfall_indices(rirs, 3, options), [])
        options["report_waterfalls"] = "all"
        self.assertEqual(waterfall_indices(rirs, 3, options), [0, 1, 2])
        options["report_max_waterfalls"] = 0
        self.assertEqual(waterfall_indices(rirs, 3, options), [])

    def test_display_mesh_is_bounded_and_retains_time_frequency_limits(self):
        frequencies, times = np.linspace(20, 300, 2000), np.linspace(0, 0.3, 200)
        levels = np.arange(200 * 2000).reshape(200, 2000)
        x, y, z = display_waterfall(frequencies, times, levels, report_options({}))
        self.assertEqual(z.shape, (24, 96))
        np.testing.assert_array_equal(x[[0, -1]], [20, 300])
        np.testing.assert_array_equal(y[[0, -1]], [0, 0.3])
        self.assertEqual(z[-1, -1], levels[-1, -1])

    def test_diffuse_maps_are_unique_by_room_and_omit_silent_rooms(self):
        result = {"summary": {"listener_spaces": ["A", "A", "B", "Corridor"]},
                  "listener_total": np.array([[1, 2], [1, 2], [0, 0], [3, 4]])}
        self.assertEqual(decay_listener_indices({}, result, report_options({})), [0, 3])

    def test_invalid_limits_and_modes_are_rejected(self):
        for options in ({"report_max_curve_points": 12}, {"report_image_dpi": 1000},
                        {"report_max_waterfalls": -1}, {"report_waterfalls": "invalid"},
                        {"report_max_curve_points": float("nan")}):
            with self.assertRaises(ValueError):
                report_options(options)


if __name__ == "__main__":
    unittest.main()
