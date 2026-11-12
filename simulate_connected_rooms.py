#!/usr/bin/env python3
"""Simulate two rooms connected through an enclosed corridor and real door openings.

Run: python simulate_connected_rooms.py --config room_config_connected.json --output output_connected
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

from connected_geometry import Geometry, SPACE_NAMES, dimensions, locate_space, plan_segments, space_boxes, validate_plan
from simulate_room_multi import validate_config as validate_acoustics


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
    cfg = json.loads(json.dumps(raw, allow_nan=False))
    plan = cfg["floor_plan"]
    validate_plan(plan)
    materials = cfg.setdefault("materials", {"wall": 0.2, "floor": 0.3, "ceiling": 0.2, "door": 0.15})
    if set(materials) != {"wall", "floor", "ceiling", "door"}:
        raise ValueError("materials must specify wall, floor, ceiling, and door")
    for key, value in materials.items():
        validate_material(value, f"materials.{key}")
    cfg["room"] = {"dimensions_m": dimensions(plan), "absorption": materials["wall"]}
    sim = cfg.setdefault("simulation", {})
    sim.setdefault("sample_rate_hz", 16000)
    sim.setdefault("duration_s", 0.5)
    sim.setdefault("max_order", 2)
    if sim["max_order"] == "auto":
        sim["max_order"] = 2
    number(sim["max_order"], "max_order", 0, 4)
    for key, default, low, high in (("n_rays", 50000, 100, 1000000),
                                    ("seed", 42, 0, 2147483647)):
        value = number(sim.setdefault(key, default), key, low, high)
        if int(value) != value:
            raise ValueError(f"{key} must be an integer")
        sim[key] = int(value)
    number(sim.setdefault("scattering", 0.15), "scattering", 0, 1)
    number(sim.setdefault("receiver_radius_m", 0.12), "receiver_radius_m", 0.01, 0.5)
    if not isinstance(sim.setdefault("ray_tracing", True), bool):
        raise ValueError("ray_tracing must be true or false")
    cfg = validate_acoustics(cfg)
    for collection in ("speakers", "listeners"):
        for item in cfg[collection]:
            if locate_space(plan, item["position_m"]) is None:
                raise ValueError(f"{item['name']} must be inside Room A, Room B, or the corridor, away from walls")
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
    if reference <= 1e-25:
        return np.full(np.shape(values), -300.0)
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


def build_component_room(cfg, geometry, component):
    import pyroomacoustics as pra

    surfaces = geometry.surfaces(component)
    walls = []
    for index, surface in enumerate(surfaces):
        material = pra.Material(cfg["materials"][surface.kind], scattering=cfg["simulation"]["scattering"])
        # Flat coefficients use PRA's native single band. Multiband materials are
        # uniformly resampled below before creating any C++ wall.
        walls.append((surface, material, f"{surface.kind}_{index}"))
    if not pra.Material.all_flat([material for _, material, _ in walls]):
        bands = pra.acoustics.AntoniOctaveFilterBank(fs=cfg["simulation"]["sample_rate_hz"])
        for _, material, _ in walls:
            material.resample(bands)
    wall_objects = [pra.wall_factory(np.ascontiguousarray(surface.corners, dtype=np.float32),
                                    np.asarray(material.absorption_coeffs),
                                    np.asarray(material.scattering_coeffs), name=name)
                    for surface, material, name in walls]
    room = pra.Room(wall_objects, fs=cfg["simulation"]["sample_rate_hz"],
                    max_order=cfg["simulation"]["max_order"],
                    ray_tracing=cfg["simulation"]["ray_tracing"], air_absorption=False)
    if cfg["simulation"]["ray_tracing"]:
        room.set_ray_tracing(n_rays=cfg["simulation"]["n_rays"],
                             receiver_radius=cfg["simulation"]["receiver_radius_m"],
                             time_thres=cfg["simulation"]["duration_s"] + 0.05,
                             hist_bin_size=0.004, energy_thres=1e-7)
    return room


def compute_component_rirs(room):
    """Build hybrid RIRs even when a receiver has no visible early image path.

    PRA 0.10's Room.compute_rir reduces an empty ISM array for such pairs.
    Use its native builders, skipping only the empty early-path contribution.
    """
    import pyroomacoustics as pra
    from pyroomacoustics.simulation import compute_ism_rir, compute_rt_rir
    from pyroomacoustics.utilities import design_highpass_filter_sos
    from scipy.signal import sosfiltfilt

    room.image_source_model()
    if room.simulator_state["rt_needed"]:
        room.ray_tracing()
    fdl = int(pra.constants.get("frac_delay_length"))
    sos = (design_highpass_filter_sos(room.fs, pra.constants.get("rir_hpf_fc"),
                                    **pra.constants.get("rir_hpf_kwargs"))
           if pra.constants.get("rir_hpf_enable") else None)
    result = []
    for m, mic in enumerate(room.mic_array.R.T):
        receivers = []
        for s, source in enumerate(room.sources):
            parts = []
            visible = room.visibility[s][m]
            if np.any(visible):
                parts.append(compute_ism_rir(source, mic, room.mic_array.directivity[m],
                                             source.directions[m], visible, fdl, room.c,
                                             room.fs, room.octave_bands,
                                             min_phase=False, air_abs_coeffs=room.air_absorption))
            if room.simulator_state["rt_needed"]:
                parts.append(compute_rt_rir(float(np.linalg.norm(source.position - mic) / room.c),
                                            room.rt_histograms[m][s], room.rt_directional_responses[m],
                                            room.rt_args["hist_bin_size"], room.rt_args["hist_bin_size_samples"],
                                            room.get_volume(), fdl, room.c, room.fs, room.octave_bands,
                                            air_abs_coeffs=room.air_absorption))
            rir = np.zeros(max([2 * fdl] + [len(part) for part in parts]))
            for part in parts:
                rir[:len(part)] += part
            receivers.append(np.ascontiguousarray(sosfiltfilt(sos, rir)) if sos is not None else rir)
        result.append(receivers)
    return result


def simulate(cfg):
    import pyroomacoustics as pra

    pra.random.seed(cfg["simulation"]["seed"])
    geometry = Geometry(cfg["floor_plan"])
    fs = cfg["simulation"]["sample_rate_hz"]
    delays = [int(round(s["delay_ms"] * fs / 1000)) for s in cfg["speakers"]]
    samples = int(math.ceil(cfg["simulation"]["duration_s"] * fs)) + max(delays) + 2
    rirs = np.zeros((len(cfg["listeners"]), len(cfg["speakers"]), samples))
    source_components = [geometry.component_at(s["position_m"]) for s in cfg["speakers"]]
    listener_components = [geometry.component_at(s["position_m"]) for s in cfg["listeners"]]
    filter_delay = int(pra.constants.get("frac_delay_length")) // 2
    notes = ["Geometric hybrid ISM/ray-tracing estimate. No diffraction or wall/door transmission. "
             "Late-tail phase is synthesized; narrow bass resonances are not wave-solver predictions."]
    wall_counts = []
    for component in range(1, geometry.count + 1):
        sources = [i for i, value in enumerate(source_components) if value == component]
        listeners = [i for i, value in enumerate(listener_components) if value == component]
        wall_counts.append(len(geometry.surfaces(component)))
        if not sources or not listeners:
            continue
        print(f"Air space {component}/{geometry.count}: {len(sources)} speakers, {len(listeners)} listeners, "
              f"{wall_counts[-1]} wall patches...", flush=True)
        room = build_component_room(cfg, geometry, component)
        for i in sources:
            room.add_source(cfg["speakers"][i]["position_m"])
        room.add_microphone_array(np.asarray([cfg["listeners"][i]["position_m"] for i in listeners]).T)
        component_rirs = compute_component_rirs(room)
        for local_listener, listener in enumerate(listeners):
            for local_source, source in enumerate(sources):
                raw = np.asarray(component_rirs[local_listener][local_source], dtype=float)[filter_delay:]
                raw = np.pad(raw, (delays[source], 0)) * 10 ** (cfg["speakers"][source]["gain_db"] / 20)
                if not np.isfinite(raw).all():
                    raise ValueError("Simulator produced non-finite data; check the geometry and settings")
                count = min(samples, len(raw))
                rirs[listener, source, :count] = raw[:count]
    for i, component in enumerate(listener_components):
        if component not in source_components:
            notes.append(f"{cfg['listeners'][i]['name']}: isolated from every speaker by closed doors; "
                         "response is zero because transmission is not modeled.")
        elif not np.any(np.abs(rirs[i]) > 1e-25):
            notes.append(f"{cfg['listeners'][i]['name']}: no paths were detected at this order/ray count; "
                         "increase rays or reflection order before interpreting the silent response.")
    if len(cfg["speakers"]) > 1:
        rirs = np.concatenate([rirs, rirs.sum(axis=1, keepdims=True)], axis=1)
        names = [s["name"] for s in cfg["speakers"]] + ["Coherent sum (same signal)"]
    else:
        names = [cfg["speakers"][0]["name"]]
    return rirs, names, 0, {
        "pyroomacoustics_version": pra.__version__, "sound_speed_m_s": float(pra.constants.get("c")),
        "reflection_order": cfg["simulation"]["max_order"],
        "listener_names": [s["name"] for s in cfg["listeners"]],
        "listener_air_spaces": listener_components, "speaker_air_spaces": source_components,
        "connected_air_spaces": geometry.count,
        "air_space_volumes_m3": [geometry.volume(i) for i in range(1, geometry.count + 1)],
        "wall_patches_per_air_space": wall_counts,
        "analysis_samples": samples, "analysis_end_s_after_emission": samples / fs,
        "waterfall_origin_s_after_emission": 0.0,
        "ray_tracing": cfg["simulation"]["ray_tracing"], "n_rays": cfg["simulation"]["n_rays"],
        "random_seed": cfg["simulation"]["seed"], "notes": notes,
    }


def plot_room(cfg, ax):
    from matplotlib.patches import Rectangle

    plan = cfg["floor_plan"]
    for key, (lower, upper) in space_boxes(plan).items():
        ax.add_patch(Rectangle(lower[:2], upper[0] - lower[0], upper[1] - lower[1],
                               color="#f4f7fb", zorder=0))
        ax.text((lower[0] + upper[0]) / 2, upper[1] - 0.25, SPACE_NAMES[key],
                ha="center", va="top", color="#667085", fontsize=10)
    walls, doors = plan_segments(plan)
    for segment in walls:
        ax.plot(segment[:, 0], segment[:, 1], color="#344054", linewidth=2)
    for key, segment, opened in doors:
        color = "#059669" if opened else "#dc2626"
        ax.plot(segment[:, 0], segment[:, 1], color=color, linewidth=4,
                linestyle="--" if opened else "-")
        ax.annotate("Open" if opened else "Closed", segment.mean(axis=0), xytext=(0, -14),
                    textcoords="offset points", ha="center", fontsize=8, color=color)
    for collection, marker in (("speakers", "s"), ("listeners", "*")):
        for index, item in enumerate(cfg[collection]):
            x, y, z = item["position_m"]
            color = f"C{(index + (2 if collection == 'listeners' else 0)) % 10}"
            ax.scatter(x, y, marker=marker, s=160 if marker == "*" else 75, color=color, zorder=4)
            ax.annotate(f"{item['name']}\nz = {z:g} m", (x, y), xytext=(8, 8),
                        textcoords="offset points", fontsize=8, color=color)
    width, length, height = dimensions(plan)
    ax.set(xlim=(-0.5, width + 0.5), ylim=(-0.5, length + 0.5),
           xlabel="Global x (m)", ylabel="Global y (m)",
           title=f"Connected rooms · enclosed corridor · ceiling {height:g} m")
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

    explanation, axis = plt.subplots(figsize=(11, 8), layout="constrained")
    axis.axis("off")
    axis.set_title("Simulation model and door states", loc="left", fontsize=18, pad=20)
    lines = ["Two rooms and an enclosed corridor; finite-thickness partitions and doorway lintels.",
             "Open doors are air openings. Closed doors are blocking leaves with no sound transmission.",
             "Geometric model: diffraction and wave-based low-frequency coupling are not simulated.",
             "Ray-traced late reverberation has synthesized phase. Treat fine bass structure as approximate.",
             "Relative levels only; shared response and waterfall references across all listeners.", ""]
    for key, door in cfg["floor_plan"]["doors"].items():
        lines.append(f"{SPACE_NAMES[key]} door: {'OPEN' if door['open'] else 'CLOSED'} · "
                     f"{door['width_m']:g} m wide × {door['height_m']:g} m high")
    lines.extend(["", f"Connected air spaces: {metadata['connected_air_spaces']}",
                  f"ISM reflection order: {metadata['reflection_order']} · Rays/source: {metadata['n_rays']}", ""])
    import textwrap
    for note in metadata["notes"]:
        lines.append(textwrap.fill(note, width=105))
    axis.text(0, 0.98, "\n\n".join(lines), va="top", fontsize=10, transform=axis.transAxes)
    figures.append(("model_notes", explanation))

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
            if not np.any(np.abs(responses[indices]) > 1e-25):
                ax.text(0.5, 0.5, "No predicted sound path · see model notes",
                        ha="center", color="#b42318", transform=ax.transAxes)
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
                 "time zero at source emission\n"
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
        "model": "3D connected rectilinear rooms; hybrid image sources and stochastic ray tracing",
        "array_axes": {"rir": ["listener", "channel", "sample"],
                       "transfer_function": ["listener", "channel", "frequency"],
                       "response_db": ["listener", "channel", "frequency"],
                       "waterfall_db": ["listener", "channel", "time", "frequency"]},
        "waterfall_time_reference": "Common source emission time, including disconnected receivers",
    })
    (output / "run_summary.json").write_text(json.dumps(metadata, indent=2) + "\n")
    if show:
        plt.show()
    for _, fig in figures:
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("room_config_connected.json"))
    parser.add_argument("--output", type=Path, default=Path("output_connected"))
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
