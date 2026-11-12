#!/usr/bin/env python3
"""Damped rectangular-room modal bass maps, placement search and small PDF reports."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys

import numpy as np


PROJECT = Path(__file__).resolve().parent
SPEED = 343.0
DENSITY = 1.2
VOLUME_VELOCITY = 1e-4
REFERENCE_FREQUENCY = 80.0
REFERENCE_PRESSURE = DENSITY * 2 * np.pi * REFERENCE_FREQUENCY * VOLUME_VELOCITY / (4 * np.pi)
EXCLUSION_RADIUS = 0.2


def numeric(value, name, minimum, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def validate_config(raw):
    cfg = json.loads(json.dumps(raw, allow_nan=False))
    if "floor_plan" in cfg:
        raise ValueError("This optimizer supports one rectangular room; connected-room layouts are not supported")
    if "subwoofers" not in cfg and "speakers" in cfg:
        cfg = {"room": {"dimensions_m": cfg["room"]["dimensions_m"]},
               "subwoofers": cfg["speakers"],
               "listeners": cfg.get("listeners", [{"name": "Seat 1", "position_m": cfg.get("listener_m")}])}
    dims = cfg["room"]["dimensions_m"]
    if not isinstance(dims, list) or len(dims) != 3:
        raise ValueError("Room dimensions must be [width, length, height]")
    for value in dims:
        numeric(value, "Room dimension (m)", 1, 20)
    for collection in ("subwoofers", "listeners"):
        items = cfg[collection]
        if not isinstance(items, list) or not 1 <= len(items) <= 12:
            raise ValueError(f"Use 1–12 {collection}")
        names = set()
        for i, item in enumerate(items):
            name = item.setdefault("name", f"{'Sub' if collection == 'subwoofers' else 'Seat'} {i + 1}")
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError(f"{collection} names must be unique and nonempty")
            names.add(name)
            position = item["position_m"]
            if not isinstance(position, list) or len(position) != 3:
                raise ValueError(f"{name} position must be [x, y, z]")
            for axis, (value, size) in enumerate(zip(position, dims)):
                numeric(value, f"{name} coordinate {axis + 1}", 0.01, size - 0.01)
            if collection == "subwoofers":
                numeric(item.setdefault("gain_db", 0), f"{name} gain (dB)", -60, 24)
                numeric(item.setdefault("delay_ms", 0), f"{name} delay (ms)", 0, 100)
                if isinstance(item.setdefault("polarity", 1), bool) or item["polarity"] not in (-1, 1):
                    raise ValueError("Polarity must be 1 or -1")
                item["polarity"] = int(item["polarity"])
                for key, default in (("enabled", True), ("locked", False)):
                    if not isinstance(item.setdefault(key, default), bool):
                        raise ValueError(f"{key} must be true or false")
    if not any(sub["enabled"] for sub in cfg["subwoofers"]):
        raise ValueError("Enable at least one subwoofer")
    for seat in cfg["listeners"]:
        if any(math.dist(seat["position_m"], sub["position_m"]) < EXCLUSION_RADIUS
               for sub in cfg["subwoofers"] if sub["enabled"]):
            raise ValueError("Keep listening positions at least 0.2 m from active subwoofers")
    for i, sub in enumerate(cfg["subwoofers"]):
        if any(math.dist(sub["position_m"], other["position_m"]) < EXCLUSION_RADIUS
               for other in cfg["subwoofers"][:i]):
            raise ValueError("Keep subwoofers at least 0.2 m apart")
    model = cfg.setdefault("model", {})
    numeric(model.setdefault("rt60_s", 0.5), "RT60 (s)", 0.1, 2.0)
    limits = model.setdefault("frequency_range_hz", [20, 160])
    if not isinstance(limits, list) or len(limits) != 2:
        raise ValueError("Frequency range must be [minimum, maximum]")
    for value in limits:
        numeric(value, "Frequency (Hz)", 10, 250)
        if int(value) != value:
            raise ValueError("Frequency limits must be whole Hz")
    if limits[1] - limits[0] < 10:
        raise ValueError("Choose a frequency band at least 10 Hz wide")
    cutoff = model.setdefault("mode_cutoff_hz", "auto")
    if cutoff != "auto":
        numeric(cutoff, "Mode cutoff (Hz)", 2 * limits[1], 900)
    map_cfg = cfg.setdefault("map", {})
    numeric(map_cfg.setdefault("height_m", cfg["listeners"][0]["position_m"][2]),
            "Map plane height (m)", 0.01, dims[2] - 0.01)
    numeric(map_cfg.setdefault("grid_spacing_m", 0.15), "Map grid spacing (m)", 0.08, 1)
    colors = map_cfg.setdefault("color_limits_db", [-35, 25])
    if not isinstance(colors, list) or len(colors) != 2:
        raise ValueError("Color limits must be [minimum, maximum]")
    for value in colors:
        numeric(value, "Color limit (dB)", -100, 100)
    if colors[1] - colors[0] < 10:
        raise ValueError("Color range must span at least 10 dB")
    options = cfg.setdefault("optimization", {})
    if options.setdefault("region", "perimeter") not in ("perimeter", "floor"):
        raise ValueError("Search region must be perimeter or floor")
    numeric(options.setdefault("grid_spacing_m", 0.5), "Search spacing (m)", 0.25, 2)
    numeric(options.setdefault("wall_clearance_m", 0.3), "Search wall clearance (m)", 0.1, min(dims[:2]) / 3)
    numeric(options.setdefault("maximum_level_drop_db", 3), "Maximum mean level drop (dB)", 0, 12)
    rounds = numeric(options.setdefault("rounds", 2), "Search rounds", 1, 4)
    if int(rounds) != rounds:
        raise ValueError("Search rounds must be a whole number")
    options["rounds"] = int(rounds)
    # Bound work before allocating dense modal matrices.
    generate_modes(cfg)
    nx, ny = map_shape(cfg)
    if nx * ny > 4000:
        raise ValueError("Map grid exceeds 4000 points; increase the grid spacing")
    if len(candidate_xy(cfg)) > 2000:
        raise ValueError("Search grid exceeds 2000 candidates; increase search spacing")
    return cfg


def fingerprint(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def generate_modes(cfg):
    dims = np.asarray(cfg["room"]["dimensions_m"], dtype=float)
    cutoff = cfg["model"]["mode_cutoff_hz"]
    cutoff = max(300.0, 3.0 * cfg["model"]["frequency_range_hz"][1]) if cutoff == "auto" else float(cutoff)
    maxima = np.floor(2 * cutoff * dims / SPEED).astype(int)
    if math.prod((maxima + 1).tolist()) > 100000:
        raise ValueError("Too many room modes for live preview; lower the mode cutoff")
    indices = np.asarray(np.meshgrid(*(np.arange(n + 1) for n in maxima), indexing="ij")).reshape(3, -1).T
    frequencies = SPEED / 2 * np.sqrt(np.sum((indices / dims) ** 2, axis=1))
    keep = frequencies <= cutoff
    indices, frequencies = indices[keep], frequencies[keep]
    if len(indices) > 3500:
        raise ValueError("More than 3500 modes; lower the cutoff or use a smaller room")
    order = np.argsort(frequencies, kind="stable")
    return indices[order], frequencies[order], cutoff


def map_shape(cfg):
    width, length, _ = cfg["room"]["dimensions_m"]
    spacing = cfg["map"]["grid_spacing_m"]
    return max(3, math.ceil(width / spacing) + 1), max(3, math.ceil(length / spacing) + 1)


def candidate_xy(cfg):
    width, length, _ = cfg["room"]["dimensions_m"]
    options = cfg["optimization"]
    margin, step = options["wall_clearance_m"], options["grid_spacing_m"]
    x = np.linspace(margin, width - margin, max(2, math.ceil((width - 2 * margin) / step) + 1))
    y = np.linspace(margin, length - margin, max(2, math.ceil((length - 2 * margin) / step) + 1))
    xx, yy = np.meshgrid(x, y)
    points = np.c_[xx.ravel(), yy.ravel()]
    if options["region"] == "perimeter":
        points = points[(np.isclose(points[:, 0], x[0]) | np.isclose(points[:, 0], x[-1])
                         | np.isclose(points[:, 1], y[0]) | np.isclose(points[:, 1], y[-1]))]
    return points


def level_db(pressure):
    return np.maximum(-120, 20 * np.log10(np.maximum(np.abs(pressure), 1e-30) / REFERENCE_PRESSURE))


class ModalRoom:
    def __init__(self, cfg, frequencies=None):
        self.cfg = cfg
        self.dimensions = np.asarray(cfg["room"]["dimensions_m"])
        lo, hi = cfg["model"]["frequency_range_hz"]
        self.frequencies = np.arange(lo, hi + 1, dtype=float) if frequencies is None else np.asarray(frequencies)
        self.indices, self.mode_frequencies, self.cutoff = generate_modes(cfg)
        omega, omega_n = 2 * np.pi * self.frequencies, 2 * np.pi * self.mode_frequencies
        damping = 3 * np.log(10) / cfg["model"]["rt60_s"]
        self.weights = ((1j * DENSITY * SPEED ** 2 * VOLUME_VELOCITY * omega / np.prod(self.dimensions))[None, :]
                        / (omega_n[:, None] ** 2 - omega[None, :] ** 2 + 2j * damping * omega[None, :]))

    def basis(self, positions):
        positions = np.atleast_2d(positions)
        result = np.ones((len(positions), len(self.indices)))
        # Normalized cosine modes have integral(phi_n^2) = room volume.
        for axis in range(3):
            result *= np.cos(np.pi * positions[:, axis, None] * self.indices[None, :, axis] / self.dimensions[axis])
        result *= np.sqrt(2.0) ** np.count_nonzero(self.indices, axis=1)
        return result

    def drive(self, sub):
        if not sub["enabled"]:
            return np.zeros(len(self.frequencies), dtype=complex)
        return (sub["polarity"] * 10 ** (sub["gain_db"] / 20)
                * np.exp(-2j * np.pi * self.frequencies * sub["delay_ms"] / 1000))

    def pressure(self, positions, subwoofers=None):
        subs = self.cfg["subwoofers"] if subwoofers is None else subwoofers
        excitation = np.zeros_like(self.weights)
        for sub in subs:
            excitation += self.basis([sub["position_m"]])[0, :, None] * self.drive(sub)[None, :]
        return self.basis(positions) @ (self.weights * excitation)

    def candidate_transfer(self, receivers, source_positions):
        # Return candidate × receiver × frequency, retaining complex phase.
        receiver_basis = self.basis(receivers)
        green = self.weights[:, None, :] * receiver_basis.T[:, :, None]
        return (self.basis(source_positions) @ green.reshape(len(self.indices), -1)).reshape(
            len(source_positions), len(receivers), len(self.frequencies))


def response_metrics(levels):
    levels = np.asarray(levels)
    ripple = float(np.sqrt(np.mean(np.var(levels, axis=-1))))
    spread = float(np.sqrt(np.mean(np.var(levels, axis=0))))
    return {"score_db": 0.7 * ripple + 0.3 * spread, "spectral_ripple_db": ripple,
            "seat_spread_db": spread, "mean_level_db": float(levels.mean()),
            "worst_seat_span_db": float(np.ptp(levels, axis=-1).max())}


def score_frequencies(cfg):
    return np.geomspace(*cfg["model"]["frequency_range_hz"], 64)


def calculate(cfg):
    model = ModalRoom(cfg)
    nx, ny = map_shape(cfg)
    width, length, _ = cfg["room"]["dimensions_m"]
    x, y = np.linspace(0, width, nx), np.linspace(0, length, ny)
    xx, yy = np.meshgrid(x, y)
    points = np.c_[xx.ravel(), yy.ravel(), np.full(xx.size, cfg["map"]["height_m"])]
    print(f"Calculating {len(model.indices)} modes, {len(points)} map points, {len(model.frequencies)} frequencies…", flush=True)
    pressure = model.pressure(points)
    levels = level_db(pressure).T.reshape(len(model.frequencies), ny, nx)
    near_source = np.zeros(len(points), dtype=bool)
    for sub in cfg["subwoofers"]:
        if sub["enabled"]:
            near_source |= np.linalg.norm(points - sub["position_m"], axis=1) < EXCLUSION_RADIUS
    levels[:, near_source.reshape(ny, nx)] = np.nan
    seats = [seat["position_m"] for seat in cfg["listeners"]]
    seat_pressure = model.pressure(seats)
    score_model = ModalRoom(cfg, score_frequencies(cfg))
    score_levels = level_db(score_model.pressure(seats))
    summary = {"model": "3D cosine-mode expansion; rigid-wall mode shapes with uniform RT60 damping",
               "mode_count": len(model.indices), "mode_cutoff_hz": model.cutoff,
               "metrics": response_metrics(score_levels),
               "level_reference": "One unit volume-velocity monopole at 1 m in free field at 80 Hz",
               "reference_pressure_pa": float(REFERENCE_PRESSURE),
               "units": "relative dB; not calibrated SPL",
               "source_assumption": "Coherent, constant volume-velocity monopoles; configured gain, delay and polarity",
               "limits": "Empty rectangular room; phenomenological uniform damping; no furniture, driver/crossover "
                         "response, flexible-wall vibration, transmission or connected-room geometry. "
                         "Finite mode cutoff; near-source map points within 0.2 m are excluded."}
    return {"frequencies_hz": model.frequencies, "x_m": x, "y_m": y, "map_db": levels.astype(np.float32),
            "seat_pressure": seat_pressure, "seat_db": level_db(seat_pressure),
            "score_frequencies_hz": score_model.frequencies, "score_seat_db": score_levels,
            "mode_indices": model.indices, "mode_frequencies_hz": model.mode_frequencies,
            "fingerprint": np.asarray(fingerprint(cfg)), "summary_json": np.asarray(json.dumps(summary))}


def optimize(cfg):
    best = copy.deepcopy(cfg)
    if not any(sub["enabled"] and not sub["locked"] for sub in best["subwoofers"]):
        raise ValueError("Unlock at least one active subwoofer for the placement search")
    model = ModalRoom(cfg, score_frequencies(cfg))
    receivers = np.asarray([seat["position_m"] for seat in cfg["listeners"]])
    current_parts = [model.pressure(receivers, [sub]) for sub in cfg["subwoofers"]]
    current = np.sum(current_parts, axis=0)
    baseline = response_metrics(level_db(current))
    if baseline["mean_level_db"] < -60:
        raise ValueError("The listening positions nearly cancel; adjust subwoofer polarity/gain before searching")
    minimum_level = baseline["mean_level_db"] - cfg["optimization"]["maximum_level_drop_db"]
    best_metrics = baseline
    xy = candidate_xy(cfg)
    candidates = {}
    for index, sub in enumerate(cfg["subwoofers"]):
        if sub["enabled"] and not sub["locked"]:
            points = np.c_[xy, np.full(len(xy), sub["position_m"][2])]
            candidates[index] = (points, model.candidate_transfer(receivers, points) * model.drive(sub)[None, None, :])
    evaluations, completed_rounds = 0, 0
    for round_index in range(cfg["optimization"]["rounds"]):
        improved = False
        for index, (points, transfers) in candidates.items():
            print(f"Search round {round_index + 1}: {best['subwoofers'][index]['name']} · {len(points)} candidates", flush=True)
            total = current[None, :, :] - current_parts[index][None, :, :] + transfers
            levels = level_db(total)
            ripple = np.sqrt(np.mean(np.var(levels, axis=2), axis=1))
            spread = np.sqrt(np.mean(np.var(levels, axis=1), axis=1))
            scores = 0.7 * ripple + 0.3 * spread
            valid = np.mean(levels, axis=(1, 2)) >= minimum_level
            for other_index, other in enumerate(best["subwoofers"]):
                if other_index != index:
                    valid &= np.linalg.norm(points - other["position_m"], axis=1) >= EXCLUSION_RADIUS
            for seat in receivers:
                valid &= np.linalg.norm(points - seat, axis=1) >= EXCLUSION_RADIUS
            scores[~valid] = np.inf
            candidate = int(np.argmin(scores))
            evaluations += len(points)
            if scores[candidate] < best_metrics["score_db"] - 1e-8:
                best["subwoofers"][index]["position_m"] = points[candidate].tolist()
                current_parts[index] = transfers[candidate].copy()
                current = total[candidate].copy()
                best_metrics = response_metrics(level_db(current))
                improved = True
        completed_rounds += 1
        if not improved:
            break
    return {"baseline_config": cfg, "best_config": validate_config(best),
            "baseline_metrics": baseline, "best_metrics": best_metrics,
            "evaluations": evaluations, "rounds_completed": completed_rounds,
            "minimum_mean_level_db": minimum_level,
            "method": "Coordinate descent on sampled positions; positions only; no global optimum guarantee",
            "objective": "0.7 × RMS within-seat spectral ripple + 0.3 × RMS seat-to-seat spread, "
                         "at 64 log-spaced frequencies, with a mean-output guard"}


def load_result(path):
    with np.load(path) as archive:
        return {key: archive[key] for key in archive.files}


def run_request(request, output):
    cfg = validate_config(request["config"])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    comparison = request.get("comparison")
    if request.get("task") == "optimize":
        comparison = optimize(cfg)
        cfg = comparison["best_config"]
        (output / "optimization.json").write_text(json.dumps(comparison, indent=2) + "\n")
    data = None
    if request.get("cached_result"):
        cached = load_result(request["cached_result"])
        if str(cached["fingerprint"]) == fingerprint(cfg):
            data = cached
    if data is None:
        data = calculate(cfg)
    np.savez_compressed(output / "map_data.npz", **data)
    (output / "subwoofer_config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (output / "summary.json").write_text(str(data["summary_json"]) + "\n")
    if request.get("task") == "report":
        from subwoofer_report import export_report

        export_report(cfg, data, output, request.get("frequency_hz", 80), comparison)
    print("Finished", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", type=Path)
    group.add_argument("--request", type=Path)
    parser.add_argument("--task", choices=("calculate", "optimize", "report"), default="report")
    parser.add_argument("--output", type=Path, default=PROJECT / "output_subwoofer")
    parser.add_argument("--frequency", type=float, default=80)
    args = parser.parse_args()
    try:
        request = (json.loads(args.request.read_text()) if args.request else
                   {"config": json.loads(args.config.read_text()), "task": args.task, "frequency_hz": args.frequency})
        run_request(request, args.output)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
