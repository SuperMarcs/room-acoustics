"""Display-only limits for lightweight reports; calculation arrays stay intact."""
from __future__ import annotations

import math
import numpy as np


REPORT_DEFAULTS = {
    "report_max_curve_points": 1200,
    "report_waterfalls": "combined",
    "report_max_waterfalls": 3,
    "report_waterfall_frequency_points": 96,
    "report_waterfall_time_slices": 24,
    "report_max_decay_time_points": 96,
    "report_image_dpi": 120,
}


def report_options(plots):
    options = {key: plots.get(key, default) for key, default in REPORT_DEFAULTS.items()}
    if options["report_waterfalls"] not in ("none", "combined", "all"):
        raise ValueError("report_waterfalls must be 'none', 'combined', or 'all'")
    limits = {"report_max_curve_points": (64, 20000), "report_max_waterfalls": (0, 50),
              "report_waterfall_frequency_points": (16, 512), "report_waterfall_time_slices": (8, 128),
              "report_max_decay_time_points": (16, 512), "report_image_dpi": (72, 200)}
    for key, (low, high) in limits.items():
        value = options[key]
        if (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
                or int(value) != value or not low <= value <= high):
            raise ValueError(f"{key} must be an integer between {low} and {high}")
        options[key] = int(value)
    return options


def display_curve(frequencies, values, bounds, max_points):
    """Keep endpoints and min/max in log-frequency buckets, within a hard limit.

    Narrow peaks and dips survive, unlike every-Nth-point subsampling. Restrict
    points to the visible range before reducing them. Inputs are never modified.
    """
    frequencies, values = np.asarray(frequencies), np.asarray(values)
    lo, hi = bounds
    selected = np.flatnonzero((frequencies >= lo) & (frequencies <= hi) & (frequencies > 0))
    if len(selected) <= max_points:
        return frequencies[selected], values[selected]
    bins = (max_points - 2) // 2
    edges = np.geomspace(frequencies[selected[0]], frequencies[selected[-1]], bins + 1)
    positions = np.searchsorted(frequencies[selected], edges)
    positions[-1] = len(selected)
    keep = [selected[0], selected[-1]]
    for start, end in zip(positions[:-1], positions[1:]):
        if end > start:
            chunk = selected[start:end]
            keep.extend([chunk[np.argmin(values[chunk])], chunk[np.argmax(values[chunk])]])
    keep = np.unique(keep)
    return frequencies[keep], values[keep]


def display_indices(count, limit):
    return np.unique(np.linspace(0, count - 1, min(count, limit), dtype=int))


def display_waterfall(frequencies, times, levels, options):
    fi = display_indices(len(frequencies), options["report_waterfall_frequency_points"])
    ti = display_indices(len(times), options["report_waterfall_time_slices"])
    return frequencies[fi], times[ti], levels[np.ix_(ti, fi)]


def waterfall_indices(rirs, n_channels, options):
    """Keep combined-response pages per listener, omitting silent channels."""
    if options["report_waterfalls"] == "none" or options["report_max_waterfalls"] == 0:
        return []
    candidates = (range(n_channels - 1, len(rirs), n_channels)
                  if options["report_waterfalls"] == "combined" else range(len(rirs)))
    return [index for index in candidates if np.any(np.abs(rirs[index]) > 1e-25)][:options["report_max_waterfalls"]]


def decay_listener_indices(cfg, result, options):
    if options["report_waterfalls"] == "none":
        return []
    seen, indices = set(), []
    for index, space in enumerate(result["summary"]["listener_spaces"]):
        if space not in seen and np.any(result["listener_total"][index] > 0):
            seen.add(space)
            indices.append(index)
    return indices[:options["report_max_waterfalls"]]


def rendering_summary(options, names, selected):
    return {**options, "waterfall_pages": len(selected),
            "waterfall_channels": [names[index] for index in selected],
            "surface_embedding": "raster image; axes and text remain vector",
            "curve_reduction": "log-frequency min/max buckets, preserving peaks and dips",
            "full_resolution_data_preserved": True}


def add_report_controls(editor, form):
    from PySide6.QtWidgets import QComboBox

    editor.report_mode_combo = QComboBox()
    for label, mode in (("Combined response only", "combined"), ("No decay plots", "none"),
                        ("All speaker channels", "all")):
        editor.report_mode_combo.addItem(label, mode)
    editor.report_mode_combo.currentIndexChanged.connect(editor._edit)
    editor.report_count_var = editor.field()
    editor.report_points_var = editor.field()
    form.addRow("PDF decay plots", editor.report_mode_combo)
    form.addRow("Max plots per decay type", editor.report_count_var)
    form.addRow("Max points per curve", editor.report_points_var)


def load_report_controls(editor):
    options = report_options(editor.cfg["plots"])
    editor.report_mode_combo.setCurrentIndex(editor.report_mode_combo.findData(options["report_waterfalls"]))
    editor.report_count_var.setText(str(options["report_max_waterfalls"]))
    editor.report_points_var.setText(str(options["report_max_curve_points"]))


def read_report_controls(editor, plots):
    plots.update(report_waterfalls=editor.report_mode_combo.currentData(),
                 report_max_waterfalls=float(editor.report_count_var.text()),
                 report_max_curve_points=float(editor.report_points_var.text()))
