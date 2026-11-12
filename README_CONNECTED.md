# Connected rooms and doors: third version

This separate version models **two rooms with no door between them**, connected
through an **enclosed corridor with a door into each room**. Both earlier versions
and their configuration files remain unchanged. It uses the same virtual environment
and installed dependencies.

PDFs now use [lightweight rendering defaults](README_REPORTS.md): fewer waterfalls,
bounded display points and rasterized surfaces. Full calculation data is retained.

```text
┌──────────────┐ ┌─────────────┐
│    Room A    │ │   Room B    │
│              │ │             │
└─────Door A───┘ └──Door B─────┘
│          Enclosed corridor   │
└──────────────────────────────┘
```

## Open the editor

Double-click **Open Connected Rooms Editor.command**, or run:

```bash
cd room-acoustics
.venv/bin/python room_gui_connected.py
```

The light theme and the original report workflow are retained.

- **Positions:** add/remove speakers and listeners, drag their markers, and edit
  their names, positions and heights. The **Space** selector moves the selected
  object into Room A, Room B, or the corridor. Position fields use coordinates
  measured from that space's southwest floor corner. JSON stores global coordinates.
- **Rooms and doors:** set each room's width and length, the corridor depth, shared
  ceiling height, and partition thickness. Set each door's width, height, distance
  from its room's west wall, and open/closed state. The plan updates immediately;
  green dashed door markers mean open and red solid markers mean closed.
- **Report:** edit surface absorption, sample rate, analysis duration, waterfall
  settings, reflection order and the number of rays per source.
- **Create report:** run the acoustic calculation in a background process. Each run
  is saved in its own dated folder under `reports_connected`. Continue editing or
  cancel while it runs. **Open PDF** opens the completed report.
- **Save/Load configuration:** save the connected-room layout and all settings as
  JSON. This version uses `room_config_connected.json`; older rectangular-room
  configurations do not describe this floor plan and should be opened with their
  original editor.

Objects retain their local room coordinates when the layout changes and move
inside their room's walls if necessary. During dragging, markers snap into the
nearest valid room/corridor interior. Objects are placed inside rooms and the
corridor, rather than within the thin door thresholds. Invalid entries disable
report creation until corrected. At least one speaker and one listener are required.

## Geometry and door behavior

Room A sits west of Room B. A solid partition of the specified thickness separates
them. Both rooms adjoin the north side of a corridor that spans their combined
width. All spaces share one flat floor and one ceiling height; the rooms may have
different lengths. This version provides this editable layout template, rather
than arbitrary wall drawing or an outdoor propagation model.

Each open doorway is a real 3D air opening through a finite-thickness partition,
including side reveals and a solid lintel above the opening. A closed door inserts
a blocking leaf into its doorway. The geometry builder computes the exact boundary
of the connected air volumes, merging planar patches without sealing openings.
Closed doors divide the layout into separate acoustic air spaces; the calculation
handles these separately.

**Closed doors transmit no sound in this model.** A listener isolated from every
speaker gets a zero impulse response and an explanatory report note. Door absorption
controls reflection at the closed leaf; it does not describe transmission loss.
The simulator does not predict real-world door leakage or sound passing through
walls, floors or ceilings.

## Acoustic method and limits

General rooms cannot use the rectangular version's efficient high-order shoebox
method. This version uses **early image sources plus ray-traced late reverberation**
with pyroomacoustics 0.10. Default settings are ISM order 2 and 50,000 rays per source.
Use orders 0–4; cost grows rapidly with the number of wall patches and reflection
order. The random seed is recorded with the report for repeatable configurations.

Ray tracing lets modeled reflection paths carry energy through open doors and
around the corridor. It does **not** model diffraction or a full low-frequency wave
field. Late reverberation is synthesized from energy histograms, with synthesized
phase. Fine bass resonances, doorway wave coupling, and coherent late-tail
interference are therefore approximate. More rays can stabilize weak paths through
narrow openings, but cannot add the missing wave physics.

Receiver radius defaults to 0.12 m. Keep listeners clear of walls where possible;
the ray-tracing receiver is a finite sphere. This is an approximate acoustic model
with omnidirectional point speakers, no speaker driver response, and relative dB
levels rather than calibrated SPL.

## Reports and data

The PDF and PNG outputs contain the full floor plan and door states, an explicit
model/limitations page, a response comparison across listeners, response/bass
detail pages for each listener, and combined-response bass waterfalls (up to three
non-silent listeners by default). Per-speaker waterfalls are optional. They use
common level references across listeners. Waterfall time zero is **source emission**,
preserving propagation delays; listeners are not aligned to their own first arrival.
The combined channel assumes all speakers reproduce the same signal.

Each run also includes the configuration snapshot, `simulation.log`, a CSV response
table, `run_summary.json` (including air-space connectivity and volume), and
`simulation_data.npz`. Array axes are:

- `rir`: listener × channel × sample.
- `transfer_function` and `response_db`: listener × channel × frequency.
- `waterfall_db`: listener × channel × time × frequency.

`listener_names` and `channel_names` label the axes; the final channel is the
coherent sum when there are multiple speakers. Silent responses are recorded as
zero in the raw data and below the display floor in dB, never as a 0 dB signal.

Run without a GUI:

```bash
.venv/bin/python simulate_connected_rooms.py --config room_config_connected.json --output output_connected
```

Run the geometry, propagation, door-state and GUI report checks:

```bash
ROOM_GUI_TEST=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -p test_connected.py -v
```

References: [pyroomacoustics room simulation and hybrid method](https://pyroomacoustics.readthedocs.io/en/pypi-release/pyroomacoustics.room.html)
and [the project's nonrectangular 3D example](https://github.com/LCAV/pyroomacoustics/blob/master/examples/room_L_shape_3d_rt.py).
