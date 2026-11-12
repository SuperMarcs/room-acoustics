#!/usr/bin/env python3
"""Rebuild a lightweight PDF from cached results, without simulating again.

Example: python rebuild_compact_report.py --report-folder reports_multi/room_...
The original report is preserved; the new report goes in its compact/ subfolder.
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
from pathlib import Path

import numpy as np


def rebuild(report_folder, output=None):
    source = Path(report_folder).resolve()
    target = Path(output).resolve() if output is not None else source / "compact"
    if source == target:
        raise ValueError("Choose a separate output folder to preserve the original report")
    metadata = json.loads((source / "run_summary.json").read_text())
    if (source / "air_path_simulation_data.npz").exists():
        module_name, data_name = "simulate_transmission", "air_path_simulation_data.npz"
    else:
        module_name = "simulate_connected_rooms" if "floor_plan" in metadata["config"] else "simulate_room_multi"
        data_name = "simulation_data.npz"
    with np.load(source / data_name) as archive:
        cached = {key: archive[key] for key in archive.files}
    if cached["rir"].ndim != 3:
        raise ValueError("This command supports multi-listener, connected-room and transmission reports")
    module = importlib.import_module(module_name)
    cfg = module.validate_config(copy.deepcopy(metadata["config"]))
    # Apply the compact preset even if an old report requested all channels.
    cfg["plots"].update(report_waterfalls="combined", report_max_waterfalls=3,
                        report_max_curve_points=1200, report_waterfall_frequency_points=96,
                        report_waterfall_time_slices=24, report_max_decay_time_points=96,
                        report_image_dpi=120)
    onset = round(metadata["waterfall_origin_s_after_emission"] * cfg["simulation"]["sample_rate_hz"])
    metadata["rebuilt_from"] = str(source)
    module.export_report(cfg, target, cached["rir"], cached["channel_names"].tolist(), onset, metadata)
    # Verify a display-only change, including FFTs, smoothing and waterfall arrays.
    with np.load(target / data_name) as rebuilt:
        for key, value in cached.items():
            if key not in rebuilt or not np.array_equal(value, rebuilt[key]):
                raise RuntimeError(f"Unexpected change in calculation data: {key}")
    if (source / "transmission_data.npz").exists():
        with np.load(source / "transmission_data.npz") as before, np.load(target / "transmission_data.npz") as after:
            for key in before.files:
                if key not in after or not np.array_equal(before[key], after[key]):
                    raise RuntimeError(f"Unexpected change in transmission data: {key}")
    return target / "acoustics_report.pdf"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report-folder", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / ".mplconfig"))
    import matplotlib

    matplotlib.use("Agg")
    try:
        print(rebuild(args.report_folder, args.output))
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
