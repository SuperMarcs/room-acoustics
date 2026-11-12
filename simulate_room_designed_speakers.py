#!/usr/bin/env python3
"""Room frequency-response reports using Speaker Studio designs.

This is a separate multi-listener version. Original room scripts are unchanged.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
from pathlib import Path
import sys
import textwrap

import numpy as np
from scipy.fft import next_fast_len, rfft, rfftfreq

import simulate_room_multi as room_model
from report_rendering import display_curve, report_options
from speaker_profile import validate_assignments, combine_responses, source_profile

WALLS = room_model.WALLS


def validate_config(raw):
    candidate = copy.deepcopy(raw)
    # This version has no decay plots. Supply safe compatibility values for the
    # shared room validator, independent of the selected frequency-report horizon.
    duration = room_model.number(candidate.get("simulation", {}).get("duration_s", 0.5),
                                 "duration_s", 0.05, 10)
    plots = candidate.setdefault("plots", {})
    plots.update(waterfall_duration_s=duration * 0.5, waterfall_window_s=duration * 0.4,
                 waterfall_step_s=min(0.01, duration * 0.2),
                 report_waterfalls="none", report_max_waterfalls=0)
    return validate_assignments(room_model.validate_config(candidate))


def simulate(cfg):
    return room_model.simulate(cfg)


def smooth_supported(freq, pressure, width):
    magnitude = np.abs(pressure)
    valid = np.isfinite(magnitude)
    if not np.any(valid):
        return np.full(magnitude.shape, np.nan)
    # Extend only for the smoothing kernel; never display or export invented data.
    filled = np.interp(freq, freq[valid], magnitude[valid])
    result = room_model.fractional_octave_smooth(freq, filled, width)
    result[~valid] = np.nan
    return result


def calculate_responses(cfg, room_rirs):
    fs = cfg["simulation"]["sample_rate_hz"]
    nfft = next_fast_len(room_rirs.shape[-1] * 2)
    freq = rfftfreq(nfft, 1 / fs)
    room_h = rfft(room_rirs, n=nfft, axis=-1)
    pressure, profiles = combine_responses(cfg, room_h, freq)
    magnitude = np.asarray([[smooth_supported(freq, h, cfg["plots"]["smoothing_octaves"])
                             for h in listener] for listener in pressure])
    flat_magnitude = np.asarray([[smooth_supported(freq, h * 4*np.pi,
                                                   cfg["plots"]["smoothing_octaves"])
                                  for h in listener] for listener in room_h])
    low, high = cfg["plots"]["frequency_range_hz"]
    select = (freq >= low) & (freq <= high)
    if not np.any(np.isfinite(magnitude[:, :, select])):
        raise ValueError("No supported response samples in the selected report frequency range.")
    reference = max(float(np.max(flat_magnitude[:, :, select])), 1e-30)
    return dict(frequency=freq, room_transfer=room_h, pressure=pressure, profiles=profiles,
                magnitude=magnitude, flat_magnitude=flat_magnitude, reference=reference,
                relative_db=room_model.db(magnitude, reference),
                flat_relative_db=room_model.db(flat_magnitude, reference),
                predicted_spl_db=room_model.db(magnitude, 20e-6))


def export_report(cfg, output, room_rirs, names, onset_sample, metadata, show=False):
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.ticker import FuncFormatter, LogLocator

    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    data = calculate_responses(cfg, room_rirs)
    freq, levels = data["frequency"], data["relative_db"]
    plots = cfg["plots"]; rendering = report_options(plots)
    bounds = plots["frequency_range_hz"]
    listeners = [x["name"] for x in cfg["listeners"]]
    n_listeners, n_channels, _ = levels.shape
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "figure.facecolor": "white", "axes.facecolor": "white",
                         "text.color": "#101828", "axes.labelcolor": "#101828",
                         "xtick.color": "#101828", "ytick.color": "#101828",
                         "axes.spines.top": False, "axes.spines.right": False})
    figures = []
    notes = [
        "Each cabinet is an equivalent omnidirectional point source carrying its fixed on-axis 1 m response. "
        "Angular radiation, toe-in, cabinet scattering and driver positions inside the room are not modelled.",
        "Each source's complex speaker response multiplies its own room transfer function before the channels are summed. "
        "Speaker gain and delay are applied in addition to the exported design.",
        "The exported common 1 m flight time is removed; room propagation is applied once. "
        "Relative levels use one flat-source room peak reference across all positions and sources.",
        "Unassigned speakers are flat 1 Pa at 1 m references. SPL data are model predictions, not calibrated measurements.",
        "This version exports frequency responses only. Stored room_rir arrays are the raw flat-source room responses, "
        "not speaker-filtered impulse responses; no speaker waterfall or audio convolution is inferred.",
        "All speakers reproduce the same signal coherently. Speaker designs are frozen inside the room configuration; "
        "load the export again after changing a design.",
    ]
    notes.extend(metadata.get("notes", []))
    summary, ax = plt.subplots(figsize=(11, 8), layout="constrained")
    ax.axis("off")
    lines = ["Speaker designs in the room", ""]
    for i, speaker in enumerate(cfg["speakers"]):
        profile = source_profile(cfg, i)
        description = (f"{profile['name']} | {profile['reference_voltage_rms']:g} V RMS | "
                       f"{profile['frequency_hz'][0]:g}–{profile['frequency_hz'][-1]:g} Hz") if profile else "Flat reference, 1 Pa at 1 m"
        lines.extend(textwrap.wrap(f"{speaker['name']}: {description}", 115))
    lines += ["", "Model and interpretation", ""]
    for note in notes:
        lines.extend(textwrap.wrap("• " + note, 118)); lines.append("")
    ax.text(0.01, 0.99, "\n".join(lines), va="top", fontsize=max(7, min(10, 490/max(len(lines), 1))),
            transform=ax.transAxes)
    figures.append(("speaker_assignments", summary))
    layout, ax = plt.subplots(figsize=(8, 8), layout="constrained")
    room_model.plot_room(cfg, ax); figures.append(("room_top_view", layout))

    profile_fig, ax = plt.subplots(figsize=(11, 5), layout="constrained")
    for i, speaker in enumerate(cfg["speakers"]):
        freefield_db = room_model.db(data["profiles"][i], 20e-6)
        ax.semilogx(*display_curve(freq, freefield_db, bounds, rendering["report_max_curve_points"]),
                    label=speaker["name"], linewidth=1.5)
    ax.set(xlim=bounds, xlabel="Frequency (Hz)", ylabel="Predicted SPL at 1 m (dB)",
           title="Exported speaker designs · gain and room distance not applied")
    ax.grid(which="both", alpha=0.2); ax.legend()
    figures.append(("speaker_design_responses", profile_fig))

    shown = levels[:, :, (freq >= bounds[0]) & (freq <= bounds[1])]
    finite = shown[np.isfinite(shown)]
    top = max(3.0, float(finite.max())+3) if len(finite) else 3.0
    bottom = min(-70.0, top-70)
    compare, axes = plt.subplots(2, 1, figsize=(11, 8), layout="constrained")
    for axis, region in zip(axes, (bounds, plots["bass_range_hz"])):
        for li, name in enumerate(listeners):
            axis.semilogx(*display_curve(freq, levels[li, -1], region, rendering["report_max_curve_points"]), label=name)
        axis.set(xlim=region, ylim=(bottom, top), ylabel="Relative level (dB)")
        axis.grid(which="both", alpha=0.2)
    axes[0].set_title("Listening-position comparison · combined designed speakers")
    axes[0].legend(fontsize=8); axes[-1].set_xlabel("Frequency (Hz)")
    figures.append(("listener_comparison", compare))

    for li, listener in enumerate(listeners):
        fig, axes = plt.subplots(2, 1, figsize=(11, 8), layout="constrained")
        for axis, region in zip(axes, (bounds, plots["bass_range_hz"])):
            for ch, name in enumerate(names):
                is_sum = len(cfg["speakers"]) > 1 and ch == n_channels-1
                axis.semilogx(*display_curve(freq, levels[li, ch], region, rendering["report_max_curve_points"]),
                              label=name, color="#101828" if is_sum else f"C{ch%10}",
                              linewidth=2 if is_sum else 1.2)
            axis.semilogx(*display_curve(freq, data["flat_relative_db"][li, -1], region,
                                       rendering["report_max_curve_points"]),
                          color="#98a2b3", linestyle="--", linewidth=1, label="Flat-source room comparison")
            axis.set(xlim=region, ylim=(bottom, top), ylabel="Relative level (dB)")
            axis.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
            axis.grid(which="both", alpha=0.2)
        axes[0].set_title(f"Speaker + room response · {listener}")
        axes[0].legend(loc="lower left", fontsize=8)
        axes[-1].set_xlabel("Frequency (Hz)")
        fig.suptitle("Common flat-source room reference · speaker and room complex responses combined", fontsize=10)
        figures.append((f"frequency_response_listener_{li+1:02d}", fig))

    with PdfPages(output / "acoustics_report.pdf") as pdf:
        for stem, fig in figures:
            fig.savefig(output / f"{stem}.png", dpi=rendering["report_image_dpi"], bbox_inches="tight")
            pdf.savefig(fig, dpi=rendering["report_image_dpi"], bbox_inches="tight")
            if not show:
                plt.close(fig)
    mask = (freq >= bounds[0]) & (freq <= bounds[1])
    labels = [f"{listener} / {name}" for listener in listeners for name in names]
    flat_levels = levels.reshape(-1, len(freq))
    predicted = data["predicted_spl_db"].reshape(-1, len(freq))
    with (output / "frequency_response.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frequency_hz"] + [f"{name}_relative_db" for name in labels]
                        + [f"{name}_predicted_spl_db" for name in labels])
        writer.writerows(zip(freq[mask], *flat_levels[:, mask], *predicted[:, mask]))
    np.savez_compressed(output / "simulation_data.npz", sample_rate_hz=cfg["simulation"]["sample_rate_hz"],
                        frequency_hz=freq, room_rir=room_rirs, room_transfer_function=data["room_transfer"],
                        transfer_function=data["pressure"], speaker_response_pa_at_1m=data["profiles"],
                        response_db=levels, predicted_spl_db=data["predicted_spl_db"],
                        listener_names=np.asarray(listeners), channel_names=np.asarray(names))
    metadata.update(config=cfg, notes=notes, response_reference_magnitude=data["reference"],
                    level_units="relative dB (common flat-source room reference); additional modelled SPL data in CSV/NPZ",
                    speaker_profiles={k: {key: v for key, v in p.items()
                                          if key not in ("frequency_hz", "pressure_real_pa", "pressure_imag_pa")}
                                      for k, p in cfg["speaker_profiles"].items()},
                    report_rendering={**rendering, "waterfall_pages": 0, "frequency_only": True},
                    model="Shoebox ISM × complex on-axis equivalent omnidirectional speaker response",
                    pressure_scaling="PRA point-source 1/(4 pi r) × 4 pi × speaker Pa at 1 m",
                    array_axes={"room_rir": ["listener", "channel", "sample"],
                                "transfer_function": ["listener", "channel", "frequency"],
                                "speaker_response_pa_at_1m": ["speaker", "frequency"]})
    (output / "run_summary.json").write_text(json.dumps(metadata, indent=2, allow_nan=False)+"\n")
    (output / "room_config.json").write_text(json.dumps(cfg, indent=2, allow_nan=False)+"\n")
    if show:
        plt.show()
    for _, fig in figures:
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("room_config_designed_speakers.json"))
    parser.add_argument("--output", type=Path, default=Path("output_designed_speakers"))
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / ".mplconfig"))
    import matplotlib
    if not args.show:
        matplotlib.use("Agg")
    try:
        cfg = validate_config(json.loads(args.config.read_text()))
        rirs, names, onset, metadata = simulate(cfg)
        export_report(cfg, args.output, rirs, names, onset, metadata, args.show)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    print(f"Report saved to {args.output.resolve() / 'acoustics_report.pdf'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
