# Room acoustics simulator

Edit a JSON file to describe a rectangular room, speakers and a listening position.
The script uses **pyroomacoustics** to calculate a 3D room impulse response for each
speaker, then exports:

- A dimensioned **2D top view**, with speaker/listener heights and listening paths.
- **Frequency response and bass detail** at the listener, for each speaker and their coherent sum.
- A **bass decay waterfall** for each speaker and their sum.
- A multipage **PDF report**, individual **PNG plots**, frequency-response **CSV**,
  raw impulse responses/complex transfer functions in **NPZ**, and a JSON run summary.

## Install and run

Use Python 3.9 or newer in a virtual environment:

```bash
cd room-acoustics
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python simulate_room.py --config room_config.json --output output
```

Add `--show` to also display interactive Matplotlib windows. Saved plots work without
a graphical desktop. If pip needs to compile pyroomacoustics, it requires a C++
compiler; on macOS install Apple's command line tools with `xcode-select --install`.

## Desktop editor

```bash
python room_gui.py
```

On macOS you can also double-click **Open Room Editor.command**, which uses the
project's `.venv`. The editor uses Qt through **PySide6**, installed by
`python -m pip install -r requirements.txt`.

- Change **width, length and height** to update the room plan immediately.
- Drag speaker squares or the listener diamond to change their **x/y** positions.
  Select any marker to enter exact coordinates, height, speaker gain and delay.
- Add or remove speakers. When reducing room size, coordinates stay fixed where
  possible; markers outside the smaller room move just inside its walls.
- Use **Materials and report** to edit surface absorption and simulation settings.
  A material field accepts a coefficient, database name, or JSON band specification.
- **Save configuration** and **Load configuration** use the same JSON format as
  the command line script. Unsaved edits are marked in the window title.
- Choose a report folder, then click **Create report**. The calculation runs in a
  separate process, keeping the editor responsive. You can cancel a running report.
- Use **Open PDF** after completion. Each run has a separate dated subfolder with
  the report, plots, data, a configuration snapshot and `simulation.log`.

Room geometry updates live; acoustic plots are calculated when you click **Create
report**. The report uses the settings captured at that click, even if you continue
editing. Invalid fields show an error and disable report creation until corrected;
the plan continues to show the last valid configuration. Sample-rate reductions
automatically cap plotted upper frequency limits at 45% of the new rate.

## Enter your room

The included `room_config.json` is an example **4.5 × 6 × 2.7 m** room with two speakers.
All dimensions and coordinates are in metres:

```json
"dimensions_m": [4.5, 6.0, 2.7]
```

Coordinates are `[x, y, z]`, measured from the floor corner at `(0, 0, 0)`:

- `x`: distance from the west wall; east wall is at `x = width`.
- `y`: distance from the south wall; north wall is at `y = length`.
- `z`: height above the floor.

For example, a speaker 1 m from the west wall, 0.8 m from the south wall, at a height
of 1.2 m has `"position_m": [1.0, 0.8, 1.2]`. To specify its position from the east
wall instead, subtract that distance from the room width. Put your ear position in
`listener_m`. Positions must be inside the room, including their heights.

Add/remove entries in `speakers` as needed. `gain_db` sets relative speaker gain;
`delay_ms` adds a nonnegative delay, rounded to the nearest sample. At 16 kHz one
sample is 0.0625 ms. Every speaker is an ideal omnidirectional point source.

### Wall absorption

`room.absorption` may be one coefficient for every surface, a material database
name, a frequency-dependent specification, or a dictionary with **all six walls**
(`west`, `east`, `south`, `north`, `floor`, `ceiling`). A numeric coefficient is
**energy absorption**: `0` is fully reflecting and `1` is fully absorbing. The
example values are illustrative, not measurements of your surfaces.

For frequency-dependent absorption, replace a coefficient with:

```json
{
  "coeffs": [0.08, 0.10, 0.15, 0.25, 0.35, 0.40],
  "center_freqs": [125, 250, 500, 1000, 2000, 4000]
}
```

Or use a pyroomacoustics database name such as `"brickwork"`. Material data and its
low-frequency extrapolation can strongly affect the predicted bass decay.

### Simulation and plot settings

| Setting | Meaning |
| --- | --- |
| `sample_rate_hz` | Default 16000; every plotted frequency must be below half this value. For a response up to 20 kHz, use 48000 and raise `frequency_range_hz`. |
| `duration_s` | Analysis length starting just before the first direct arrival; default 0.5 s. Longer runs resolve narrow resonances and longer decays. |
| `max_order` | `"auto"` includes all shoebox image paths within the analysis horizon using a conservative reflection-count bound. An integer trades accuracy for speed. |
| `frequency_range_hz` | Full response plot range. |
| `smoothing_octaves` | Default 1/12-octave RMS smoothing on a log-frequency grid; use `0` for raw response. |
| `bass_range_hz` | Bass-detail and waterfall frequency range; default 20–300 Hz. |
| `waterfall_duration_s` | Last waterfall window's start time; default 0.3 s. |
| `waterfall_window_s` | FFT window length; default 0.2 s, giving nominal 5 Hz resolution. |
| `waterfall_step_s` | Time between waterfall slices; default 0.01 s. |
| `waterfall_floor_db` | Waterfall display floor; default −60 dB. |

`duration_s` must cover `waterfall_duration_s + waterfall_window_s`. A longer FFT
window improves frequency resolution at the expense of time resolution. Zero
padding makes the surface smoother but does not improve physical resolution.
Reflection computation grows roughly with the **cube of the order**; long durations
in small rooms can take substantial memory. Start with the example settings.

## Interpret the plots

The frequency response comes from the FFT of each finite impulse response. All
curves use the **same peak reference**, preserving differences between speakers.
The CSV contains the same smoothed levels as the plots; the NPZ also contains raw
complex spectra and uncalibrated impulse responses.

The coherent sum assumes all speakers reproduce **the same signal**, with the
configured gains and delays. Its interference pattern does not describe unrelated
left/right stereo content. Physical arrival-time differences are preserved.

The waterfall uses fixed-length **forward** windows through the impulse response.
Each window begins flat and tapers over its final 20%, so the first slice retains
the direct impulse. All slices and speakers use a **common waterfall reference**;
the waterfall and response plots have separate references. No slice is separately
normalized. Time zero starts just before the first direct arrival to include the
fractional-delay pulse; the exact offset is recorded in `run_summary.json`.

This is a geometric image-source estimate for a rectangular, empty room. It does
not model furniture, wall transmission, diffraction, speaker directivity, driver
response, crossover filters, or frequency-dependent reflection phase. Bass results
are useful for comparing placements under these assumptions; accurate low-frequency
predictions need measured room responses or a suitable wave-based solver. The
waterfall shows **decay at the listener**, rather than a spatial bass map.

Levels are **relative dB, not calibrated dB SPL**. The response is limited by the
finite analysis horizon; the run summary reports discarded generated energy and
warns if a manually chosen reflection order is insufficient. With longer durations,
increase the order or retain `"auto"` and check that the results converge.

API references: [room simulation](https://pyroomacoustics.readthedocs.io/en/pypi-release/pyroomacoustics.room.html)
and [materials and simulation parameters](https://pyroomacoustics.readthedocs.io/en/pypi-release/pyroomacoustics.parameters.html).

## Check the calculation

```bash
python -m unittest discover -s tests -v
```

To also exercise dragging, input validation and the background report button
without opening visible windows (macOS/Linux):

```bash
ROOM_GUI_TEST=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

Load exported arrays without pickle:

```python
import numpy as np
data = np.load("output/simulation_data.npz")
print(data["names"])
print(data["rir"].shape)  # channels × samples, last channel is the sum for multiple speakers
```

## License

This project's original code and documentation are licensed under the
[MIT License](LICENSE). Dependencies retain their own licenses; see
[Third-party notices](THIRD_PARTY_NOTICES.md).
