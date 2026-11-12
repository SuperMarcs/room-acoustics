"""Small, image-backed heatmap reports for the live subwoofer placement app."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import numpy as np

from subwoofer_model import ModalRoom, fingerprint, level_db


def export_report(cfg, data, output, frequency_hz=80, comparison=None):
    os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / ".mplconfig"))
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    if str(data["fingerprint"]) != fingerprint(cfg):
        raise ValueError("Cached map does not match the current configuration")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    summary = json.loads(str(data["summary_json"]))
    frequencies = data["frequencies_hz"]
    selected = int(np.argmin(abs(frequencies - frequency_hz)))
    map_indices = [selected]
    for frequency in (40, 80, 120):
        if frequencies[0] <= frequency <= frequencies[-1]:
            index = int(np.argmin(abs(frequencies - frequency)))
            if index not in map_indices and len(map_indices) < 3:
                map_indices.append(index)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "figure.facecolor": "white", "axes.spines.top": False,
                         "axes.spines.right": False, "axes.titleweight": "bold"})
    width, length, height = cfg["room"]["dimensions_m"]
    lo, hi = cfg["map"]["color_limits_db"]
    before_levels = None
    if comparison and comparison.get("best_config") == cfg:
        previous = comparison["baseline_config"]
        before_levels = level_db(ModalRoom(previous, frequencies).pressure([s["position_m"] for s in previous["listeners"]]))
    else:
        comparison = None
    files = []
    with PdfPages(output / "subwoofer_report.pdf") as pdf:
        def save(stem, fig):
            fig.savefig(output / f"{stem}.png", dpi=120, bbox_inches="tight")
            pdf.savefig(fig, dpi=120, bbox_inches="tight")
            plt.close(fig)
            files.append(stem + ".png")

        for index in map_indices:
            frequency = float(frequencies[index])
            fig, ax = plt.subplots(figsize=(10, 7), layout="constrained")
            image = ax.imshow(data["map_db"][index], origin="lower", extent=(0, width, 0, length),
                              vmin=lo, vmax=hi, cmap="viridis", interpolation="bilinear", aspect="equal",
                              rasterized=True)
            for i, sub in enumerate(cfg["subwoofers"]):
                x, y, z = sub["position_m"]
                color = f"C{i % 10}" if sub["enabled"] else "#98a2b3"
                ax.scatter(x, y, marker="s", s=95, c=color, edgecolors="white", linewidths=1.5)
                ax.annotate(f"{sub['name']} · z {z:g} m", (x, y), xytext=(7, 8), textcoords="offset points",
                            fontsize=8, bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none"})
            for seat in cfg["listeners"]:
                x, y, _ = seat["position_m"]
                ax.scatter(x, y, marker="*", s=120, c="white", edgecolors="#101828", linewidths=0.8)
                ax.annotate(seat["name"], (x, y), xytext=(7, 8), textcoords="offset points", fontsize=8,
                            bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "none"})
            ax.set(xlabel="x / width (m)", ylabel="y / length (m)",
                   title=f"Bass level map · {frequency:g} Hz · plane z = {cfg['map']['height_m']:g} m")
            fig.colorbar(image, ax=ax, shrink=0.8, label="Relative level (dB), fixed reference", extend="both")
            save(f"heatmap_{frequency:g}Hz", fig)

        fig, ax = plt.subplots(figsize=(11, 6), layout="constrained")
        for i, seat in enumerate(cfg["listeners"]):
            color = f"C{i % 10}"
            if before_levels is not None:
                ax.semilogx(frequencies, before_levels[i], "--", alpha=0.5, color=color, linewidth=1,
                             label=seat["name"] + " · before")
            ax.semilogx(frequencies, data["seat_db"][i], color=color, linewidth=1.5,
                         label=seat["name"] + (" · optimized" if before_levels is not None else ""))
        ax.set(xlim=(frequencies[0], frequencies[-1]), xlabel="Frequency (Hz)", ylabel="Relative level (dB)",
               title="Listening-position bass response" + (" · before / after" if before_levels is not None else ""))
        ax.grid(True, which="both", alpha=0.2)
        ax.legend(fontsize=8, ncol=2)
        save("listening_responses", fig)

        fig, ax = plt.subplots(figsize=(11, 8))
        ax.axis("off")
        ax.set_title("Subwoofer placement and model summary", loc="left", fontsize=17, pad=18)
        metrics = summary["metrics"]
        lines = [f"Room: {width:g} × {length:g} × {height:g} m · RT60: {cfg['model']['rt60_s']:g} s",
                 f"Band: {frequencies[0]:g}–{frequencies[-1]:g} Hz · {summary['mode_count']} modes through {summary['mode_cutoff_hz']:g} Hz",
                 "Relative pressure level: fixed reference = one unit monopole at 1 m, at 80 Hz (not calibrated SPL).",
                 "Subwoofers are ideal, coherent, constant-volume-velocity monopoles; no driver/crossover response.", "",
                 "Objective at 64 log-spaced frequencies: 70% spectral ripple + 30% seat-to-seat spread.",
                 f"Current score: {metrics['score_db']:.2f} dB · RMS ripple: {metrics['spectral_ripple_db']:.2f} dB",
                 f"Seat spread: {metrics['seat_spread_db']:.2f} dB · Mean level: {metrics['mean_level_db']:.2f} dB"]
        if comparison:
            before, after = comparison["baseline_metrics"], comparison["best_metrics"]
            lines.extend(["", f"Search: {comparison['evaluations']} candidate evaluations; {comparison['rounds_completed']} rounds.",
                          f"Score before / after: {before['score_db']:.2f} / {after['score_db']:.2f} dB (lower is better).",
                          f"Mean output guard: at least {comparison['minimum_mean_level_db']:.2f} dB.",
                          "Sampled coordinate-descent search; positions only. This is not a proven global optimum."])
        lines.append("")
        for sub in cfg["subwoofers"]:
            lines.append(f"{sub['name']}: {sub['position_m']} m · {sub['gain_db']:g} dB · delay {sub['delay_ms']:g} ms · "
                         f"polarity {sub['polarity']:+d}" + (" · muted" if not sub["enabled"] else ""))
        lines.extend(["", "Approximation: rectangular rigid-wall mode shapes, uniform phenomenological damping and finite cutoff.",
                      "Real wall impedance, furnishings, connected rooms, transmission and subwoofer directivity are omitted.",
                      "Within 0.2 m of active sources, map points are excluded. Results need confirmation with room measurements."])
        ax.text(0, 0.98, "\n".join(lines), va="top", fontsize=9, linespacing=1.6, transform=ax.transAxes)
        save("model_summary", fig)
    with (output / "listening_response.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frequency_hz"] + [s["name"] + "_relative_db" for s in cfg["listeners"]])
        writer.writerows(zip(frequencies, *data["seat_db"]))
    details = {"config": cfg, "model": summary, "selected_frequency_hz": float(frequencies[selected]),
               "heatmap_frequencies_hz": [float(frequencies[i]) for i in map_indices],
               "plot_files": files, "optimization": comparison,
               "data_axes": {"map_db": ["frequency", "y", "x"], "seat_pressure": ["listener", "frequency"]},
               "pdf": "Raster heatmaps at 120 dpi; bounded frequency curves; no 3D waterfall surfaces"}
    (output / "run_summary.json").write_text(json.dumps(details, indent=2) + "\n")
