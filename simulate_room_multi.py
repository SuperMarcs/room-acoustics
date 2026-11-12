#!/usr/bin/env python3
"""Simulate a shoebox room at multiple listening positions.

Run: python simulate_room_multi.py --config room_config_multi.json --output output_multi
All coordinates are [x, y, z] in metres; see README.md for assumptions.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
from scipy.fft import next_fast_len, rfft, rfftfreq

from report_rendering import (report_options, display_curve, display_waterfall,
                              waterfall_indices, rendering_summary)


WALLS = ("west", "east", "south", "north", "floor", "ceiling")
SIM_DEFAULTS = {"sample_rate_hz": 16000, "duration_s": 0.5, "max_order": "auto"}
PLOT_DEFAULTS = {
    "frequency_range_hz": [20, 7000],
    "smoothing_octaves": 1 / 12,
    "bass_range_hz": [20, 300],
    "waterfall_duration_s": 0.3,
    "waterfall_window_s": 0.2,
    "waterfall_step_s": 0.01,
    "waterfall_floor_db": -60,
}


def number(value, name, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if low is not None and value < low:
        raise ValueError(f"{name} must be >= {low}")
    if high is not None and value > high:
        raise ValueError(f"{name} must be <= {high}")
    return value


def vector(value, name):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must be [x, y, z]")
    return [number(v, name) for v in value]


def validate_material(value, name):
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{name}: material name cannot be empty")
    elif isinstance(value, dict):
        coeffs, freqs = value.get("coeffs"), value.get("center_freqs")
        if not isinstance(coeffs, list) or not coeffs:
            raise ValueError(f"{name}.coeffs must be a nonempty list")
        if not isinstance(freqs, list) or len(freqs) != len(coeffs):
            raise ValueError(f"{name}: center_freqs and coeffs must have equal length")
        for coeff in coeffs:
            number(coeff, name + ".coeffs", 0, 1)
        for freq in freqs:
            number(freq, name + ".center_freqs", 1e-6)
        if any(a >= b for a, b in zip(freqs, freqs[1:])):
            raise ValueError(f"{name}.center_freqs must be strictly increasing")
    else:
        number(value, name, 0, 1)


def validate_config(raw):
    """Fill optional defaults and reject invalid geometry before simulation."""
    cfg = json.loads(json.dumps(raw, allow_nan=False))
    dims = vector(cfg["room"]["dimensions_m"], "room.dimensions_m")
    if min(dims) <= 0:
        raise ValueError("Room dimensions must be positive")
    absorption = cfg["room"].setdefault("absorption", 0.2)
    if isinstance(absorption, dict) and "coeffs" not in absorption:
        if set(absorption) != set(WALLS):
            raise ValueError("Wall absorption must specify all six walls: " + ", ".join(WALLS))
        for wall, value in absorption.items():
            validate_material(value, f"room.absorption.{wall}")
    else:
        validate_material(absorption, "room.absorption")

    def position(value, name):
        pos = vector(value, name)
        if any(p <= 0 or p >= d for p, d in zip(pos, dims)):
            raise ValueError(f"{name} must be strictly inside the room, including its height")
        return pos

    if "listeners" not in cfg and "listener_m" in cfg:
        cfg["listeners"] = [{"name": "Listener 1", "position_m": cfg.pop("listener_m")}]
    cfg.pop("listener_m", None)
    listeners = cfg["listeners"]
    if not isinstance(listeners, list) or not listeners:
        raise ValueError("listeners must be a nonempty list")
    listener_names = set()
    for i, listener in enumerate(listeners):
        name = listener.setdefault("name", f"Listener {i + 1}")
        if not isinstance(name, str) or not name.strip() or name in listener_names:
            raise ValueError("Listener names must be nonempty and unique")
        listener_names.add(name)
        position(listener["position_m"], f"listeners[{i}].position_m")
    speakers = cfg["speakers"]
    if not isinstance(speakers, list) or not speakers:
        raise ValueError("speakers must be a nonempty list")
    names = set()
    for i, speaker in enumerate(speakers):
        name = speaker.setdefault("name", f"Speaker {i + 1}")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Speaker names must be nonempty and unique")
        names.add(name)
        pos = position(speaker["position_m"], f"speakers[{i}].position_m")
        if any(np.linalg.norm(np.subtract(pos, listener["position_m"])) < 0.01
               for listener in listeners):
            raise ValueError("Every speaker must be at least 1 cm from every listener")
        number(speaker.setdefault("gain_db", 0), f"{name}.gain_db", -120, 60)
        number(speaker.setdefault("delay_ms", 0), f"{name}.delay_ms", 0, 1000)

    sim = cfg["simulation"] = {**SIM_DEFAULTS, **cfg.get("simulation", {})}
    fs = number(sim["sample_rate_hz"], "sample_rate_hz", 2000, 192000)
    if int(fs) != fs:
        raise ValueError("sample_rate_hz must be an integer")
    sim["sample_rate_hz"] = int(fs)
    number(sim["duration_s"], "duration_s", 0.05, 10)
    order = sim["max_order"]
    if order != "auto":
        number(order, "max_order", 0, 1000)
        if int(order) != order:
            raise ValueError("max_order must be an integer or 'auto'")
        sim["max_order"] = int(order)

    plots = cfg["plots"] = {**PLOT_DEFAULTS, **cfg.get("plots", {})}
    for key in ("frequency_range_hz", "bass_range_hz"):
        bounds = plots[key]
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError(f"{key} must be [minimum, maximum]")
        lo = number(bounds[0], key, 1e-6)
        hi = number(bounds[1], key, 1e-6)
        if not lo < hi < fs / 2:
            raise ValueError(f"{key} must be increasing and below Nyquist ({fs / 2:g} Hz)")
    number(plots["smoothing_octaves"], "smoothing_octaves", 0, 1)
    for key in ("waterfall_duration_s", "waterfall_window_s", "waterfall_step_s"):
        number(plots[key], key, 1 / fs, 10)
    if plots["waterfall_window_s"] * fs < 16:
        raise ValueError("waterfall_window_s must contain at least 16 samples")
    if plots["waterfall_step_s"] > plots["waterfall_duration_s"]:
        raise ValueError("waterfall_step_s must not exceed waterfall_duration_s")
    if plots["waterfall_duration_s"] + plots["waterfall_window_s"] > sim["duration_s"] + 1e-9:
        raise ValueError("duration_s must cover waterfall_duration_s + waterfall_window_s")
    number(plots["waterfall_floor_db"], "waterfall_floor_db", -150, -1)
    cfg["plots"].update(report_options(plots))
    return cfg


def fractional_octave_smooth(freqs, magnitude, width):
    """RMS average on a log-frequency grid with equal weight per octave."""
    if width == 0:
        return magnitude.copy()
    positive = freqs > 0
    log_f = np.log2(freqs[positive])
    grid = np.linspace(log_f[0], log_f[-1], max(4096, len(log_f)))
    power = np.interp(grid, log_f, magnitude[positive] ** 2)
    # scipy's uniform filter avoids a quadratic per-frequency averaging loop.
    from scipy.ndimage import uniform_filter1d

    bins = max(1, int(round(width / (grid[1] - grid[0]))))
    smoothed = uniform_filter1d(power, size=bins, mode="nearest")
    out = magnitude.copy()
    out[positive] = np.sqrt(np.maximum(np.interp(log_f, grid, smoothed), 0))
    return out


def db(values, reference):
    return 20 * np.log10(np.maximum(np.abs(values), 1e-30) / max(reference, 1e-30))


def waterfall(rirs, fs, onset_sample, plots):
    """Fixed-length forward FFT windows, preserving level between time slices.

    A flat start retains direct sound. Only the final 20% is cosine tapered.
    Windows are never independently normalized; zero padding only samples the FFT.
    """
    width = int(round(plots["waterfall_window_s"] * fs))
    step = max(1, int(round(plots["waterfall_step_s"] * fs)))
    last = int(round(plots["waterfall_duration_s"] * fs))
    offsets = np.arange(0, last + 1, step)
    if onset_sample + offsets[-1] + width > rirs.shape[1]:
        raise ValueError("RIR does not cover every complete waterfall window")
    window = np.ones(width)
    taper = max(2, width // 5)
    window[-taper:] = (1 + np.cos(np.linspace(0, np.pi, taper))) / 2
    nfft = next_fast_len(width * 4)
    freqs = rfftfreq(nfft, 1 / fs)
    lo, hi = plots["bass_range_hz"]
    select = (freqs >= lo) & (freqs <= hi)
    if select.sum() < 2:
        raise ValueError("Bass range contains fewer than two FFT bins; increase the window")
    values = np.asarray([
        [np.abs(rfft(rir[onset_sample + offset:onset_sample + offset + width] * window,
                     n=nfft))[select] for offset in offsets]
        for rir in rirs
    ])
    return freqs[select], offsets / fs, db(values, float(values.max()))


def simulate(cfg):
    import pyroomacoustics as pra

    dims = np.asarray(cfg["room"]["dimensions_m"], dtype=float)
    listeners = np.asarray([listener["position_m"] for listener in cfg["listeners"]])
    fs = cfg["simulation"]["sample_rate_hz"]
    # Leave the sound speed at PRA's documented default, 343 m/s.
    speed = float(pra.constants.get("c"))
    distances = np.asarray([[float(np.linalg.norm(np.asarray(s["position_m"]) - listener))
                            for s in cfg["speakers"]] for listener in listeners])
    delays = [int(round(s["delay_ms"] * fs / 1000)) for s in cfg["speakers"]]
    arrivals = distances / speed + np.asarray(delays)[None, :] / fs
    first_arrival = float(np.min(arrivals))
    # Retain the full initial fractional-delay pulse in the first waterfall slice.
    filter_delay = int(pra.constants.get("frac_delay_length")) // 2
    onset_sample = max(0, int(math.floor(first_arrival * fs)) - filter_delay)
    latest_onset = max(0, int(math.floor(float(np.max(np.min(arrivals, axis=1))) * fs)) - filter_delay)
    samples = latest_onset + int(math.ceil(cfg["simulation"]["duration_s"] * fs)) + 2
    horizon = samples / fs
    # In an unfolded shoebox, reflection counts satisfy
    # sum(n_i) <= path_length * ||1 / dimensions||_2 + 3.
    # Thus this conservative order includes all image paths within the horizon.
    recommended_order = math.ceil((speed * (horizon + filter_delay / fs))
                                  * float(np.linalg.norm(1 / dims)) + 3)
    requested_order = cfg["simulation"]["max_order"]
    order = recommended_order if requested_order == "auto" else requested_order
    notes = []
    if order < recommended_order:
        notes.append(f"Reflection order {order} is below the conservative order "
                     f"{recommended_order}; late response/decay may be incomplete.")
    if order > 180:
        print(f"Note: reflection order {order} can require substantial memory and time.", flush=True)

    absorption = cfg["room"]["absorption"]
    if isinstance(absorption, dict) and "coeffs" not in absorption:
        materials = {name: pra.Material(value) for name, value in absorption.items()}
    else:
        materials = pra.Material(absorption)
    print(f"Simulating {len(cfg['speakers'])} speaker(s) and {len(listeners)} listener(s), "
          f"{fs} Hz, reflection order {order}...", flush=True)
    room = pra.ShoeBox(dims, fs=fs, materials=materials, max_order=order,
                       air_absorption=False, use_rand_ism=False)
    for speaker in cfg["speakers"]:
        room.add_source(speaker["position_m"])
    room.add_microphone_array(listeners.T)
    room.compute_rir()
    rirs = np.zeros((len(listeners), len(cfg["speakers"]), samples))
    tail_ratios = np.zeros(distances.shape)
    for listener_index, listener in enumerate(cfg["listeners"]):
        for i, speaker in enumerate(cfg["speakers"]):
            # Preserve both propagation differences and configured speaker delays.
            raw = np.asarray(room.rir[listener_index][i], dtype=float)[filter_delay:]
            raw = np.pad(raw, (delays[i], 0)) * 10 ** (speaker["gain_db"] / 20)
            count = min(samples, len(raw))
            rirs[listener_index, i, :count] = raw[:count]
            total_energy = float(np.sum(raw ** 2))
            tail_ratios[listener_index, i] = float(np.sum(raw[samples:] ** 2) / total_energy) if total_energy else 0.0
            if tail_ratios[listener_index, i] > 0.001:
                notes.append(f"{listener['name']} / {speaker['name']}: more than 0.1% of the generated "
                             "RIR energy is outside the analysis horizon; increase duration_s.")
    if len(cfg["speakers"]) > 1:
        all_rirs = np.concatenate([rirs, rirs.sum(axis=1, keepdims=True)], axis=1)
        names = [s["name"] for s in cfg["speakers"]] + ["Coherent sum (same signal)"]
    else:
        all_rirs = rirs
        names = [cfg["speakers"][0]["name"]]
    return all_rirs, names, onset_sample, {
        "pyroomacoustics_version": pra.__version__,
        "sound_speed_m_s": speed,
        "reflection_order": order,
        "conservative_order_for_horizon": recommended_order,
        "listener_names": [listener["name"] for listener in cfg["listeners"]],
        "speaker_listener_distances_m": distances.tolist(),
        "listener_first_arrivals_s": np.min(arrivals, axis=1).tolist(),
        "effective_speaker_delays_ms": [delay / fs * 1000 for delay in delays],
        "analysis_samples": samples,
        "analysis_end_s_after_emission": horizon,
        "waterfall_origin_s_after_emission": onset_sample / fs,
        "generated_energy_fraction_after_horizon": tail_ratios.tolist(),
        "notes": notes,
    }


def plot_room(cfg, ax):
    from matplotlib.patches import Rectangle

    width, length, height = cfg["room"]["dimensions_m"]
    ax.add_patch(Rectangle((0, 0), width, length, fill=False, linewidth=2.5, color="#344054"))
    for i, speaker in enumerate(cfg["speakers"]):
        x, y, z = speaker["position_m"]
        color = f"C{i % 10}"
        for listener in cfg["listeners"]:
            lx, ly, _ = listener["position_m"]
            ax.plot([x, lx], [y, ly], "--", color=color, alpha=0.18)
        ax.scatter(x, y, marker="s", s=90, color=color, zorder=3)
        ax.annotate(f"{speaker['name']}\nz = {z:g} m", (x, y), xytext=(8, 10),
                    textcoords="offset points", fontsize=9)
    for index, listener in enumerate(cfg["listeners"]):
        pos = listener["position_m"]
        color = f"C{(index + len(cfg['speakers'])) % 10}"
        ax.scatter(*pos[:2], marker="*", s=220, color=color, zorder=4)
        ax.annotate(f"{listener['name']}\nz = {pos[2]:g} m", pos[:2], xytext=(10, 8),
                    textcoords="offset points", fontsize=9, color=color)
    ax.text(width / 2, -0.55, f"South wall · {width:g} m", ha="center", fontsize=9)
    ax.text(width / 2, length + 0.3, f"North wall · height {height:g} m", ha="center", fontsize=9)
    ax.text(-0.4, length / 2, f"West · {length:g} m", rotation=90, va="center", fontsize=9)
    ax.text(width + 0.3, length / 2, "East", rotation=90, va="center", fontsize=9)
    ax.set(xlim=(-0.8, width + 0.8), ylim=(-0.8, length + 0.8),
           xlabel="x / width (m)", ylabel="y / length (m)", title="Room layout · top view")
    ax.set_aspect("equal")
    ax.grid(alpha=0.15)


def export_report(cfg, output, rirs, names, onset_sample, metadata, show=False):
    import matplotlib.pyplot as plt
    from matplotlib import colors, cm
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.ticker import FuncFormatter, LogLocator

    listener_names = [listener["name"] for listener in cfg["listeners"]]
    channel_names = list(names)
    n_listeners, n_channels, n_samples = rirs.shape
    rirs_by_listener = rirs
    rirs = rirs.reshape(-1, n_samples)
    names = [f"{listener} / {channel}" for listener in listener_names for channel in channel_names]
    fs = cfg["simulation"]["sample_rate_hz"]
    plots = cfg["plots"]
    report = report_options(plots)
    nfft = next_fast_len(rirs.shape[1] * 2)
    freqs = rfftfreq(nfft, 1 / fs)
    responses = rfft(rirs, n=nfft, axis=1)
    magnitude = np.asarray([fractional_octave_smooth(freqs, np.abs(h), plots["smoothing_octaves"])
                            for h in responses])
    lo, hi = plots["frequency_range_hz"]
    select = (freqs >= lo) & (freqs <= hi)
    # One common reference preserves relative levels and coherent summation gain.
    reference = float(magnitude[:, select].max())
    response_db = db(magnitude, reference)
    wf_freqs, times, waterfall_db = waterfall(rirs, fs, onset_sample, plots)
    floor = plots["waterfall_floor_db"]

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "figure.facecolor": "white", "axes.titleweight": "bold"})
    figures = []
    layout, ax = plt.subplots(figsize=(8, 8), layout="constrained")
    plot_room(cfg, ax)
    figures.append(("room_top_view", layout))

    smoothing = plots["smoothing_octaves"]
    if smoothing and abs(1 / smoothing - round(1 / smoothing)) < 1e-6:
        description = f"1/{round(1 / smoothing)}-octave RMS smoothing"
    else:
        description = f"{smoothing:g}-octave RMS smoothing" if smoothing else "Unsmoothed"
    groups = [("listener_comparison", "Listening position comparison · " + channel_names[-1],
               [i * n_channels + n_channels - 1 for i in range(n_listeners)])]
    groups.extend((f"frequency_response_listener_{i + 1:02d}", f"Frequency response · {listener}",
                   list(range(i * n_channels, (i + 1) * n_channels)))
                  for i, listener in enumerate(listener_names))
    for stem, heading, indices in groups:
        response_fig, axes = plt.subplots(2, 1, figsize=(11, 8))
        response_fig.subplots_adjust(left=0.09, right=0.98, bottom=0.09, top=0.88, hspace=0.32)
        comparison = stem == "listener_comparison"
        for ax, bounds, title in zip(axes, [plots["frequency_range_hz"], plots["bass_range_hz"]],
                                    [heading, "Bass detail"]):
            for line_index, i in enumerate(indices):
                is_sum = n_channels > 1 and i % n_channels == n_channels - 1
                label = listener_names[i // n_channels] if comparison else channel_names[i % n_channels]
                ax.semilogx(*display_curve(freqs, response_db[i], bounds, report["report_max_curve_points"]), label=label,
                            color=f"C{line_index % 10}" if comparison or not is_sum else "#101828",
                            linestyle="--" if line_index % 2 and not is_sum else "-",
                            linewidth=1.8 if is_sum else 1.15)
            ax.set(xlim=bounds, ylim=(-70, 3), ylabel="Relative level (dB)")
            ax.set_title(title, loc="center", x=0.5, pad=10)
            ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 3, 5, 7)))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
            ax.grid(True, which="both", alpha=0.18)
        axes[0].legend(loc="lower left", fontsize=8)
        axes[-1].set_xlabel("Frequency (Hz)", loc="center")
        response_fig.suptitle(description + " · common peak reference across all listeners · uncalibrated", fontsize=10)
        figures.append((stem, response_fig))

    # Display a bounded selection; keep every channel in the data export.
    selected_waterfalls = waterfall_indices(rirs, n_channels, report)
    for index in selected_waterfalls:
        name = names[index]
        # Explicit margins keep 3D axis labels inside both PNG and PDF canvases.
        fig = plt.figure(figsize=(12, 8))
        ax = fig.add_axes([0.12, 0.18, 0.67, 0.70], projection="3d")
        colorbar_ax = fig.add_axes([0.86, 0.27, 0.018, 0.47])
        display_freqs, display_times, display_levels = display_waterfall(wf_freqs, times, waterfall_db[index], report)
        ff, tt = np.meshgrid(np.log10(display_freqs), display_times * 1000)
        levels = np.clip(display_levels, floor, 0)
        norm = colors.Normalize(vmin=floor, vmax=0)
        ax.plot_surface(ff, tt, levels, cmap="magma", norm=norm,
                        rcount=len(display_times), ccount=len(display_freqs), linewidth=0,
                        edgecolor="none", antialiased=False, rasterized=True)
        ticks = [f for f in (20, 30, 40, 50, 70, 100, 150, 200, 300, 500, 1000)
                 if wf_freqs[0] <= f <= wf_freqs[-1]]
        ax.set_xticks(np.log10(ticks), labels=[str(f) for f in ticks])
        ax.set(xlabel="Frequency (Hz, log scale)", ylabel="Window start time (ms)",
               zlim=(floor, 0))
        fig.text(0.035, 0.54, "Relative level (dB)", rotation=90, va="center", ha="center")
        fig.suptitle(f"Bass decay waterfall · {name}", y=0.94, fontsize=15, fontweight="bold")
        ax.view_init(elev=27, azim=-125)
        ax.set_box_aspect((1.8, 1.2, 1))
        fig.colorbar(cm.ScalarMappable(norm=norm, cmap="magma"), cax=colorbar_ax,
                     label="dB, common waterfall peak reference")
        fig.text(0.5, 0.055,
                 f"Forward window {plots['waterfall_window_s'] * 1000:g} ms · "
                 f"nominal frequency resolution {1 / plots['waterfall_window_s']:g} Hz · "
                 "time zero before earliest arrival across all listeners\n"
                 "Common level reference across all listeners, sources and slices",
                 ha="center", fontsize=8)
        figures.append((f"bass_waterfall_listener_{index // n_channels + 1:02d}_channel_{index % n_channels + 1:02d}", fig))

    output.mkdir(parents=True, exist_ok=True)
    with PdfPages(output / "acoustics_report.pdf") as pdf:
        for stem, fig in figures:
            fig.savefig(output / f"{stem}.png", dpi=report["report_image_dpi"], bbox_inches="tight")
            pdf.savefig(fig, dpi=report["report_image_dpi"], bbox_inches="tight")
            if not show:
                plt.close(fig)
    with (output / "frequency_response.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frequency_hz"] + [f"{name}_relative_db" for name in names])
        writer.writerows(zip(freqs[select], *response_db[:, select]))
    np.savez_compressed(output / "simulation_data.npz", sample_rate_hz=fs,
                        listener_names=np.asarray(listener_names), channel_names=np.asarray(channel_names),
                        names=np.asarray(names), rir=rirs_by_listener, frequency_hz=freqs,
                        transfer_function=responses.reshape(n_listeners, n_channels, -1),
                        response_db=response_db.reshape(n_listeners, n_channels, -1),
                        waterfall_frequency_hz=wf_freqs, waterfall_time_s=times,
                        waterfall_db=waterfall_db.reshape(n_listeners, n_channels, *waterfall_db.shape[1:]))
    metadata.update({
        "config": cfg,
        "report_rendering": rendering_summary(report, names, selected_waterfalls),
        "response_reference_magnitude": reference,
        "frequency_response_smoothing": "RMS power average, equal log-frequency weight",
        "waterfall_nominal_resolution_hz": 1 / plots["waterfall_window_s"],
        "level_units": "relative dB; not calibrated SPL",
        "sum_assumption": "All speakers reproduce the same signal coherently, with configured gains and delays",
        "model": "3D rectangular shoebox, deterministic image sources, omnidirectional point sources/receiver",
        "array_axes": {"rir": ["listener", "channel", "sample"],
                       "transfer_function": ["listener", "channel", "frequency"],
                       "response_db": ["listener", "channel", "frequency"],
                       "waterfall_db": ["listener", "channel", "time", "frequency"]},
        "waterfall_time_reference": "Common origin before the earliest arrival across all listeners",
    })
    (output / "run_summary.json").write_text(json.dumps(metadata, indent=2) + "\n")
    if show:
        plt.show()
    for _, fig in figures:
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("room_config_multi.json"))
    parser.add_argument("--output", type=Path, default=Path("output_multi"))
    parser.add_argument("--show", action="store_true", help="Also open interactive plot windows")
    args = parser.parse_args()
    # Keep Matplotlib's cache within the project rather than the user's home.
    os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / ".mplconfig"))
    import matplotlib

    if not args.show:
        matplotlib.use("Agg")
    try:
        cfg = validate_config(json.loads(args.config.read_text()))
        rirs, names, onset, metadata = simulate(cfg)
        export_report(cfg, args.output, rirs, names, onset, metadata, show=args.show)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    print(f"Report saved to {args.output.resolve() / 'acoustics_report.pdf'}")
    for note in metadata["notes"]:
        print(f"Note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
