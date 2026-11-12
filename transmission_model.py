"""Lumped, diffuse-field energy exchange across doors and separating walls.

This is a room-average energy model, with no pressure phase or point-position
resolution. It is deliberately separate from the pyroomacoustics air-path RIRs.
"""
from __future__ import annotations

import copy
import csv
import math

import numpy as np
from scipy.sparse.linalg import expm_multiply

from connected_geometry import SPACES, SPACE_NAMES, locate_space, space_boxes
from report_rendering import report_options, decay_listener_indices, display_indices


LINKS = ("door_a", "door_b", "wall_ab", "wall_ac", "wall_bc")
LINK_NAMES = {"door_a": "Door A", "door_b": "Door B", "wall_ab": "Room A / Room B wall",
              "wall_ac": "Room A / corridor wall", "wall_bc": "Room B / corridor wall"}
DEFAULT_BANDS = [63, 125, 250, 500, 1000, 2000, 4000]


def default_settings():
    # Illustrative values, not a tested door or wall construction.
    return {"enabled": True, "center_freqs_hz": DEFAULT_BANDS.copy(),
            "loss_db": {key: ([15, 18, 22, 25, 28, 30, 32] if key.startswith("door")
                               else [25, 30, 35, 40, 45, 50, 55]) for key in LINKS}}


def validate_settings(cfg):
    result = copy.deepcopy(cfg)
    settings = result.setdefault("transmission", default_settings())
    if not isinstance(settings, dict):
        raise ValueError("transmission must be an object")
    if not isinstance(settings.setdefault("enabled", True), bool):
        raise ValueError("transmission.enabled must be true or false")
    bands = settings.setdefault("center_freqs_hz", DEFAULT_BANDS.copy())
    if not isinstance(bands, list) or len(bands) < 2 or len(bands) > 40:
        raise ValueError("Transmission requires 2–40 frequency bands")
    for value in bands:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Transmission band frequencies must be finite positive numbers")
    if any(a >= b for a, b in zip(bands, bands[1:])):
        raise ValueError("Transmission band frequencies must be increasing")
    losses = settings.setdefault("loss_db", default_settings()["loss_db"])
    if not isinstance(losses, dict) or set(losses) != set(LINKS):
        raise ValueError("Transmission losses must specify " + ", ".join(LINKS))
    for key in LINKS:
        value = losses[key]
        if not isinstance(value, list):
            value = [value] * len(bands)
        if len(value) != len(bands):
            raise ValueError(f"{LINK_NAMES[key]} needs one loss value per band")
        for entry in value:
            if entry is not None and (isinstance(entry, bool) or not isinstance(entry, (int, float))
                                      or not math.isfinite(entry) or not 0 <= entry <= 150):
                raise ValueError(f"{LINK_NAMES[key]}: use 0–150 dB, or null for blocked")
        losses[key] = value
    if settings["enabled"]:
        # This also rejects non-conservative absorption/transmission budgets and
        # a non-decaying room network before a report process is started.
        exchange_matrices(result)
    return result


def absorption_at(value, bands):
    import pyroomacoustics as pra

    material = pra.Material(float(value) if isinstance(value, (int, float)) else value)
    coeffs = np.asarray(material.absorption_coeffs, dtype=float)
    if len(coeffs) == 1:
        return np.full(len(bands), coeffs[0])
    centers = np.asarray(material.energy_absorption["center_freqs"], dtype=float)
    return np.interp(np.log2(bands), np.log2(centers), coeffs)


def interface_data(cfg):
    plan = cfg["floor_plan"]
    bands = cfg["transmission"]["center_freqs_hz"]
    a, b, h = plan["room_a_width_m"], plan["room_b_width_m"], plan["height_m"]
    da = plan["doors"]["room_a"]["width_m"] * plan["doors"]["room_a"]["height_m"]
    db = plan["doors"]["room_b"]["width_m"] * plan["doors"]["room_b"]["height_m"]
    specs = [("door_a", 0, 2, da, plan["doors"]["room_a"]["open"], "door"),
             ("door_b", 1, 2, db, plan["doors"]["room_b"]["open"], "door"),
             ("wall_ab", 0, 1, min(plan["room_a_length_m"], plan["room_b_length_m"]) * h, False, "wall"),
             ("wall_ac", 0, 2, a * h - da, False, "wall"),
             ("wall_bc", 1, 2, b * h - db, False, "wall")]
    result = []
    for key, i, j, area, opened, kind in specs:
        losses = cfg["transmission"]["loss_db"][key]
        tau = np.ones(len(bands)) if opened else np.asarray([0 if loss is None else 10 ** (-loss / 10) for loss in losses])
        if not cfg["transmission"]["enabled"] and not opened:
            tau = np.zeros(len(bands))
        result.append({"key": key, "i": i, "j": j, "area_m2": area, "open": opened, "kind": kind, "tau": tau})
    return result


def exchange_matrices(cfg):
    """Q acts on total energy E: dE/dt = Q E + P.

    Symmetric boundary flux is c*S*tau*(E_i/V_i - E_j/V_j)/4.
    Transmission redistributes energy; only absorption removes it.
    """
    bands = np.asarray(cfg["transmission"]["center_freqs_hz"], dtype=float)
    boxes = space_boxes(cfg["floor_plan"])
    sizes = np.asarray([np.subtract(boxes[key][1], boxes[key][0]) for key in SPACES])
    volumes = np.prod(sizes, axis=1)
    alpha = {key: absorption_at(value, bands) for key, value in cfg["materials"].items()}
    absorbed_area = np.zeros((len(bands), 3))
    for i, (width, length, height) in enumerate(sizes):
        floor_area = width * length
        absorbed_area[:, i] = ((alpha["floor"] + alpha["ceiling"]) * floor_area
                               + alpha["wall"] * 2 * height * (width + length))
    interfaces = interface_data(cfg)
    for link in interfaces:
        if link["key"].startswith("door"):
            # Replace the generic wall's absorption by the closed leaf's, or
            # remove it altogether at an open aperture. No door-area double count.
            difference = -alpha["wall"] if link["open"] else alpha["door"] - alpha["wall"]
            for i in (link["i"], link["j"]):
                absorbed_area[:, i] += difference * link["area_m2"]
        if not link["open"] and link["area_m2"] > 0:
            budget = alpha[link["kind"]] + link["tau"]
            if np.any(budget > 1 + 1e-12):
                raise ValueError(f"{LINK_NAMES[link['key']]}: absorption + transmission exceeds 1; "
                                 "increase its TL or lower absorption")
    speed = 343.0
    matrices = np.zeros((len(bands), 3, 3))
    for i in range(3):
        matrices[:, i, i] = -speed * absorbed_area[:, i] / (4 * volumes[i])
    for link in interfaces:
        i, j = link["i"], link["j"]
        conductance = speed * link["area_m2"] * link["tau"] / 4
        matrices[:, i, j] += conductance / volumes[j]
        matrices[:, j, i] += conductance / volumes[i]
        matrices[:, i, i] -= conductance / volumes[i]
        matrices[:, j, j] -= conductance / volumes[j]
    if np.any(np.max(np.linalg.eigvals(matrices).real, axis=1) >= -1e-10):
        raise ValueError("The diffuse model needs absorption in every room or a connection to an absorbing room")
    return matrices, volumes, absorbed_area, interfaces


def power_db(values, reference):
    values = np.asarray(values)
    if reference <= 0:
        return np.full(values.shape, -300.0)
    return np.where(values > 0, 10 * np.log10(np.maximum(values, 1e-300) / reference), -300.0)


def solve_transmission(cfg):
    if not cfg["transmission"]["enabled"]:
        return None
    matrices, volumes, absorbed_area, interfaces = exchange_matrices(cfg)
    source_zones = [SPACES.index(locate_space(cfg["floor_plan"], item["position_m"])) for item in cfg["speakers"]]
    listener_zones = [SPACES.index(locate_space(cfg["floor_plan"], item["position_m"])) for item in cfg["listeners"]]
    powers = np.zeros((3, len(source_zones)))
    for i, zone in enumerate(source_zones):
        powers[zone, i] = 10 ** (cfg["speakers"][i]["gain_db"] / 10)
    # Density axes: band, zone, source. Equal relative emitted power per band.
    steady = np.asarray([np.linalg.solve(-matrix, powers) / volumes[:, None] for matrix in matrices])
    steady = np.maximum(steady, 0)
    combined = steady.sum(axis=2)
    reference = float(combined.max())
    times = np.linspace(0, cfg["simulation"]["duration_s"], 201)
    decay = np.asarray([expm_multiply(matrix, combined[b] * volumes,
                                      start=0, stop=times[-1], num=len(times)) / volumes[None, :]
                        for b, matrix in enumerate(matrices)])
    decay = np.maximum(decay, 0)
    # This estimate has no phase: sum source energies, never pressures.
    listener_density = steady[:, listener_zones, :].transpose(1, 2, 0)
    listener_total = combined[:, listener_zones].T
    listener_decay = decay[:, :, listener_zones].transpose(2, 1, 0)
    summary = {
        "enabled": True, "method": "three-zone diffuse-field energy exchange",
        "equation": "dE/dt = Q E + P; interface flux = c*S*tau*(E_i/V_i-E_j/V_j)/4",
        "source_assumption": "Equal relative emitted power in each band; sources sum incoherently",
        "position_resolution": "Room average: listeners in the same space have identical transmission estimates",
        "phase_and_timing": "No pressure phase, propagation delay, or coherent transmitted RIR is predicted",
        "decay_initial_condition": "All sources at steady state, then switched off simultaneously at time zero",
        "center_freqs_hz": cfg["transmission"]["center_freqs_hz"],
        "space_names": [SPACE_NAMES[key] for key in SPACES], "volumes_m3": volumes.tolist(),
        "reference_density": reference,
        "listener_spaces": [SPACE_NAMES[SPACES[zone]] for zone in listener_zones],
        "listener_total_relative_db": power_db(listener_total, reference).tolist(),
        "effective_tau": {link["key"]: link["tau"].tolist() for link in interfaces},
        "interface_areas_m2": {link["key"]: link["area_m2"] for link in interfaces},
        "limits": "Diffuse-field approximation, mainly above the modal region. No diffraction, flanking, "
                  "structural vibration, outdoor leakage, or measured-construction inference. "
                  "Doorway reveal volumes are neglected. TL defaults are illustrative.",
    }
    return {"summary": summary, "bands": np.asarray(cfg["transmission"]["center_freqs_hz"]),
            "matrices": matrices, "volumes": volumes, "absorbed_area": absorbed_area,
            "interfaces": interfaces, "steady_density": steady, "listener_density": listener_density,
            "listener_total": listener_total, "reference": reference, "times": times,
            "listener_decay": listener_decay}


def export_transmission_data(cfg, output, result):
    if result is None:
        return
    np.savez_compressed(output / "transmission_data.npz",
                        frequency_hz=result["bands"], time_s=result["times"],
                        listener_names=np.asarray([item["name"] for item in cfg["listeners"]]),
                        speaker_names=np.asarray([item["name"] for item in cfg["speakers"]]),
                        listener_source_density=result["listener_density"],
                        listener_total_density=result["listener_total"],
                        listener_total_relative_db=power_db(result["listener_total"], result["reference"]),
                        listener_decay_density=result["listener_decay"],
                        listener_decay_relative_db=power_db(result["listener_decay"], result["reference"]),
                        exchange_matrices=result["matrices"], volumes_m3=result["volumes"],
                        absorbed_area_m2=result["absorbed_area"], reference_density=result["reference"])
    with (output / "transmission_levels.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["listener", "space", "frequency_hz"]
                        + [f"{item['name']}_relative_db" for item in cfg["speakers"]]
                        + ["incoherent_total_relative_db"])
        source_levels = power_db(result["listener_density"], result["reference"])
        totals = power_db(result["listener_total"], result["reference"])
        for i, listener in enumerate(cfg["listeners"]):
            for band_index, frequency in enumerate(result["bands"]):
                writer.writerow([listener["name"], result["summary"]["listener_spaces"][i], frequency]
                                + source_levels[i, :, band_index].tolist() + [totals[i, band_index]])


def transmission_figures(cfg, result):
    """Yield one figure at a time so reports need not hold extra figures open."""
    if result is None:
        return
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FixedLocator, FuncFormatter

    bands, reference = result["bands"], result["reference"]
    report = report_options(cfg["plots"])
    fig, axes = plt.subplots(2, 1, figsize=(11, 8))
    fig.subplots_adjust(left=0.09, right=0.97, bottom=0.13, top=0.90, hspace=0.36)
    levels = power_db(result["listener_total"], reference)
    for i, listener in enumerate(cfg["listeners"]):
        axes[0].semilogx(bands, levels[i], "o-", label=f"{listener['name']} · {result['summary']['listener_spaces'][i]}")
    axes[0].set(title="Diffuse transmission estimate · room-average received levels",
                ylabel="Relative energy level (dB)", ylim=(-100, 3))
    axes[0].legend(fontsize=8)
    for link in result["interfaces"]:
        tau = link["tau"]
        tl = np.where(tau > 0, -10 * np.log10(np.maximum(tau, 1e-300)), np.nan)
        label = LINK_NAMES[link["key"]] + (" · open" if link["open"] else "")
        axes[1].semilogx(bands, tl, "o-", label=label)
    finite_losses = [-10 * math.log10(tau) for link in result["interfaces"] for tau in link["tau"] if tau > 0]
    loss_limit = max(10, math.ceil(max(finite_losses, default=0) / 10) * 10 + 5)
    axes[1].set(title="Effective partition transmission loss · higher means less transmitted energy",
                xlabel="Band center frequency (Hz)", ylabel="Transmission loss (dB)", ylim=(0, loss_limit))
    axes[1].legend(fontsize=7, ncol=2)
    for ax in axes:
        ax.xaxis.set_major_locator(FixedLocator(bands))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
        ax.grid(alpha=0.2)
    fig.text(0.5, 0.025, "Separate statistical estimate · sources add as energy · identical within each space\n"
             "Illustrative TL defaults: replace with measured band data. Bass/modal detail is not predicted.",
             ha="center", fontsize=9)
    yield "transmission_comparison", fig

    for i in decay_listener_indices(cfg, result, report):
        listener = cfg["listeners"][i]
        fig, ax = plt.subplots(figsize=(11, 6))
        fig.subplots_adjust(left=0.10, right=0.87, bottom=0.17, top=0.87)
        # No fabricated fine-frequency bins: each row is a supplied band.
        floor = cfg["plots"]["waterfall_floor_db"]
        log_bands = np.log(bands)
        band_edges = np.exp(np.r_[log_bands[0] - (log_bands[1] - log_bands[0]) / 2,
                                  (log_bands[:-1] + log_bands[1:]) / 2,
                                  log_bands[-1] + (log_bands[-1] - log_bands[-2]) / 2])
        time_indices = display_indices(len(result["times"]), report["report_max_decay_time_points"])
        times_ms = result["times"][time_indices] * 1000
        time_edges = np.r_[0, (times_ms[:-1] + times_ms[1:]) / 2, times_ms[-1]]
        mesh = ax.pcolormesh(time_edges, band_edges,
                             power_db(result["listener_decay"][i, time_indices], reference).T,
                             shading="flat", cmap="magma", vmin=floor, vmax=0,
                             rasterized=True, linewidth=0, antialiased=False)
        ax.set_yscale("log")
        ax.set_yticks(bands, labels=[f"{value:g}" for value in bands])
        ax.set(xlabel="Time after all sources switch off (ms)", ylabel="Band center frequency (Hz)",
               title=f"Diffuse transmitted-energy decay · {listener['name']} ({result['summary']['listener_spaces'][i]})")
        cax = fig.add_axes([0.89, 0.19, 0.018, 0.63])
        fig.colorbar(mesh, cax=cax, label="Relative energy level (dB), shared reference")
        fig.text(0.5, 0.035, "Initial state: steady excitation, then sources off · room-average statistical decay\n"
                 "This is not a pressure impulse response or a prediction of exact bass resonances.", ha="center", fontsize=9)
        yield f"transmission_decay_listener_{i + 1:02d}", fig
