# Lightweight PDF reports

The multi-listener, connected-room and transmission editors now generate compact
reports by default. Existing saved configurations automatically get these defaults;
you do not need to edit their JSON files. Restart an editor to see the new controls.

- **Combined-response waterfalls only**, with a maximum of three waterfall pages.
  Silent responses are omitted. Individual-speaker waterfall pages are omitted by
  default; speaker frequency-response curves remain in the report.
- **At most 1,200 displayed points per frequency curve**, restricted to the visible
  range. Log-frequency buckets retain their minimum and maximum, preserving narrow
  peaks and dips rather than simply keeping every Nth point.
- **At most 96 frequency points × 24 time slices** per displayed waterfall.
- **Raster images for dense surfaces**, embedded at 120 dpi in the PDF. Axes and
  labels remain vector text, avoiding thousands of PDF polygons on every page.
- Transmission decay maps are also rasterized, limited to 96 displayed time points,
  and shown once per distinct room. Silent rooms and duplicate room averages are
  omitted. At most three of these maps are included.

These limits affect plots only. Impulse responses, FFTs, complete waterfall arrays,
transmission estimates and CSV/NPZ exports retain their full calculation resolution.
The run summary records the effective rendering limits and selected waterfall pages.
The original single-listener editor is unchanged.

## GUI controls

Under **Materials and report** in the multi-listener editor, or **Report** in the
connected-room and transmission editors:

- **PDF decay plots:** choose combined response only, no decay plots, or all speaker
  channels. "No decay plots" also omits the transmission decay maps; the frequency
  and transmission-level comparisons remain.
- **Max plots per decay type:** default 3; enter 0 to omit decay pages. The cap applies
  separately to geometric waterfalls and diffuse transmission maps. Increase it if
  you need every listener/source combination.
- **Max points per curve:** default 1200; use a smaller value for simpler PDF curves.

## Lighter copies of existing reports

The saved examples and existing dated runs have new **compact/** subfolders with
rebuilt PDFs. The original reports remain in place. Rebuilding uses the saved
calculation arrays, without rerunning the acoustic simulation, and verifies that
the calculation arrays are unchanged.

For another cached report:

```bash
cd room-acoustics
.venv/bin/python rebuild_compact_report.py --report-folder reports_multi/room_...
```

The smaller PDF is written to `reports_multi/room_.../compact/acoustics_report.pdf`.
You can supply `--output another_directory` to choose a separate destination.

## Advanced JSON settings

These optional keys belong inside `plots`:

```json
{
  "report_waterfalls": "combined",
  "report_max_waterfalls": 3,
  "report_max_curve_points": 1200,
  "report_waterfall_frequency_points": 96,
  "report_waterfall_time_slices": 24,
  "report_max_decay_time_points": 96,
  "report_image_dpi": 120
}
```

`report_waterfalls` accepts `combined`, `none`, or `all`. Other existing simulation,
smoothing and waterfall-window settings continue to control the calculation itself.

Rasterization follows Matplotlib's supported mixed vector/image export workflow:
[rasterization documentation](https://matplotlib.org/stable/gallery/misc/rasterization_demo.html).
