# Live subwoofer placement studio

A separate desktop app for **live bass heatmaps**, frequency exploration, multiple
subwoofers/listening seats, a placement search, and compact PDF reports. Existing
room, connected-room and transmission scripts remain unchanged. It uses the
already installed NumPy, SciPy, Matplotlib and PySide6 packages.

Double-click **Open Subwoofer Studio.command**, or run:

```bash
cd room-acoustics
.venv/bin/python subwoofer_gui.py
```

## Use the live view

1. Enter the rectangular room's width, length and ceiling height. Add or remove
   subwoofers and listening seats. Coordinates are `[x, y, z]` in metres from the
   southwest floor corner: x from the west wall, y from the south wall, z above floor.
2. Drag the **square subs** and **diamond seats**, or select them to enter exact
   coordinates. Sub controls include gain, delay, polarity, active/mute, and a lock
   that keeps that sub fixed during optimization.
3. Press **Play**. The app calculates all whole-Hz maps in the selected band and
   shows both the heatmap and the listening-seat frequency responses. Play enables
   automatic recalculation shortly after you edit or drag; it does not play audio.
4. Move the **frequency slider**, or enter a frequency in the adjacent spin box.
   This selects an already calculated map and moves the response-plot cursor;
   it does not start a new acoustic calculation.
5. Under **Map and search**, set the map's height plane, grid spacing, assumed RT60,
   frequency range and color limits. The heatmap is a horizontal slice; seat markers
   show projected x/y locations, while the response plot uses each seat's actual z.

**Pause** stops automatic refresh while retaining the current map for frequency
exploration. After further edits, press Play to calculate the new layout. Invalid
entries show an error and disable calculation/report buttons. Stale results from
an older layout are discarded. Calculation and report work run in a separate
process with a two-thread numerical-library limit to keep the editor responsive.

The dB reference and color limits stay fixed across frequencies and placements.
Default colors span −35 to +25 relative dB. Colors outside that range are clipped
for display; the underlying levels are retained. Hover over the map for the nearest
grid sample's level. Map locations within 0.2 m of an active sub are gray/excluded.

## Search for a placement

Set the listening positions you want to improve, then press **Optimize placement**.
The search moves the active, unlocked subs in x/y, retaining their configured height,
gain, delay and polarity. It tests either perimeter candidates or candidates across
the whole floor at the chosen spacing and wall clearance. It uses coordinate descent
for a bounded number of rounds, retaining the current position if no improvement
is found. The result is the best found by that sampled search, not a guaranteed
global optimum.

The objective, evaluated at **64 log-spaced frequencies**, is:

```text
score = 0.7 × RMS spectral ripple within seats
      + 0.3 × RMS variation between seats
```

Lower is better. The mean output must remain within the configured allowed drop
from the starting placement (default 3 dB), so the search cannot simply reduce
overall output to appear smoother. Spectral ripple is computed after removing each
seat's band-average level; seat spread is the variation across seats at each
frequency. A tradeoff can improve the score while worsening one individual metric.

The chosen placement is applied and previewed automatically. **Undo optimization**
restores the previous configuration. Manual edits clear this comparison/undo context;
the current position then becomes the starting point for another search. Subwoofers
and seats must remain at least 0.2 m apart, and subwoofers must stay 0.2 m apart.

## Model and level interpretation

This app uses an **analytical damped 3D rectangular-room modal expansion**, rather
than ray tracing, to model bass standing waves. Cosine mode shapes and their
eigenfrequencies depend on room dimensions; source and receiver positions control
how those modes are excited and received. This is the same class of rectangular
Green-function reference described in [COMSOL's modal acoustics explanation](https://www.comsol.com/blogs/modeling-room-acoustics-using-a-hybrid-approach/).
The implementation is independent Python code; it does not require COMSOL or MATLAB.

- Boundaries use rigid-wall mode shapes with uniform phenomenological damping.
  The assumed amplitude decay constant is `3*ln(10)/RT60`. RT60 is an input, not a
  measured or inferred treatment specification.
- Each active sub is an ideal **constant-volume-velocity monopole**. All subs play
  the same coherent signal, with the specified gain, delay and polarity. The sum
  retains phase, so interference and polarity inversion affect the map.
- The mode cutoff defaults to `max(300, 3 × upper_band_frequency)` Hz. A finite
  cutoff limits accuracy, especially near sources. You can raise the cutoff and
  check whether placement conclusions remain stable; work/memory limits prevent
  oversized live calculations.
- Levels are **relative dB, not calibrated SPL**. The fixed pressure reference is
  one unit monopole at 1 m in free field at 80 Hz. No frequency or placement is
  independently peak-normalized. This is pressure level, not a perceptual loudness
  measure in sones or phons.
- Furniture, detailed wall impedance, transmission, flexible-wall vibration,
  driver/crossover response, connected-room geometry and actual cabinet size are
  omitted. Use the search to compare placements under these assumptions and verify
  the final choice with measurements.

The frequency band can span 10–250 Hz, at least 10 Hz wide. Typical defaults are
20–160 Hz, a 0.15 m map grid, and a 0.5 s RT60. Larger rooms, high cutoffs or fine
grids can exceed the resource limits; increase grid spacing or lower the cutoff.

You may load an earlier **single rectangular-room** configuration: its speakers
become subs, seats are retained, and the modal settings get defaults. Existing wall
absorption settings are not used by this model. Connected-room configurations need
their original editors and cannot be imported into a single rectangular model.

## Reports

Press **Create report** to capture the current layout and slider frequency. The
worker reuses matching cached results where possible. You can keep editing; the
report uses the configuration captured when clicked. **Open PDF** opens the finished
report in its own dated folder under `reports_subwoofer`.

Reports contain at most three heatmap pages, listening-seat frequency responses,
and a model/placement summary. After a search, the curves and scores include a
before/after comparison, provided you have not manually edited that placement.
Heatmaps are raster images at 120 dpi; there are no dense 3D waterfall surfaces.

Each report includes `subwoofer_report.pdf`, plot PNGs, `subwoofer_config.json`,
`listening_response.csv`, `map_data.npz`, `run_summary.json`, and a worker log. The
saved map axes are **frequency × y × x**; seat pressure/level arrays are
**listener × frequency**. Complex seat pressures retain phase. Masks are NaN in
the map array, not a fabricated silence level.

Command-line report:

```bash
.venv/bin/python subwoofer_model.py --config subwoofer_config.json --frequency 80 --output output_subwoofer
```

Use `--task calculate` for maps/data only or `--task optimize` to search and export
the chosen layout, maps and `optimization.json`.

Numerical and GUI workflow checks:

```bash
OPENBLAS_NUM_THREADS=2 ROOM_GUI_TEST=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -p test_subwoofer.py -v
```
