# Multiple-listener version

This is a separate copy of the room simulator and desktop editor. The original
`room_gui.py`, `simulate_room.py`, and `room_config.json` remain unchanged.
It uses the same installed dependencies and the same readable light theme.

PDFs now use [lightweight rendering defaults](README_REPORTS.md): fewer waterfalls,
bounded display points and rasterized surfaces. Full calculation data is retained.

Double-click **Open Multi Listener Editor.command**, or run:

```bash
cd room-acoustics
.venv/bin/python room_gui_multi.py
```

Use **Add listener** to place another listening position. Select a listener in the
list or click its diamond on the room plan to edit its name, exact x/y position,
and ear height. Drag any listener to move it. Selecting a listener shows the
paths from the speakers to that listener. **Remove** deletes the selected speaker
or listener; at least one of each is required. All listeners move inside the walls
if the room is reduced. Speakers must stay at least 1 cm from every listener.

**Create report** captures every speaker and listener in the current configuration.
You can continue editing while it calculates. Every run goes into its own dated
folder under `reports_multi`, with a configuration snapshot and simulation log.
**Open PDF** opens the completed report.

The report contains:

- A top view with all speakers and named listener positions.
- A comparison of the combined speaker response across listeners (or the single
  speaker response if there is only one speaker).
- A full-range response and bass detail page for each listener, showing individual
  speakers and their coherent sum.
- Bass-decay waterfalls for combined speaker responses (up to three non-silent
  listeners by default); individual-speaker waterfalls are optional in the GUI.
- CSV response data, raw arrays in NPZ, individual PNG plots, and a JSON summary.

All response curves use **one shared level reference** across all listeners and
speakers. Waterfalls use their own shared reference across every listener, speaker,
and time slice. Positions are not independently normalized, so level differences
remain visible. Waterfall time starts from one common origin just before the
earliest direct arrival anywhere in the room, preserving propagation-time
differences between positions. The raw impulse responses retain a common emission
time origin. No responses are averaged between listeners.

The coherent sum assumes speakers play the same signal. As in the original
version, this is a rectangular-room image-source estimate, with relative dB levels.
See [README.md](README.md) for acoustic assumptions and plot settings. More listeners
increase calculation time and report length.

## Configuration and command line

The default `room_config_multi.json` starts with two listeners. The new format is:

```json
"listeners": [
  {"name": "Listener 1", "position_m": [2.25, 3.8, 1.2]},
  {"name": "Listener 2", "position_m": [3.1, 4.5, 1.2]}
]
```

The multi-listener editor can also load the original version's JSON files, importing
`listener_m` as one named listener. Saving writes the multiple-listener format;
use a new filename to keep your original configuration.

Generate a report without opening the GUI:

```bash
.venv/bin/python simulate_room_multi.py --config room_config_multi.json --output output_multi
```

In `simulation_data.npz`, `rir` has axes **listener × channel × sample**;
`transfer_function` and `response_db` use **listener × channel × frequency**;
`waterfall_db` uses **listener × channel × time × frequency**. `listener_names` and
`channel_names` label these axes. For multiple speakers, the final channel is their
coherent sum.

```python
import numpy as np
data = np.load("output_multi/simulation_data.npz")
print(data["listener_names"])
second_listener_rir = data["rir"][1]
```

Run the multiple-listener numerical and GUI checks headlessly:

```bash
ROOM_GUI_TEST=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -p test_multi.py -v
```
