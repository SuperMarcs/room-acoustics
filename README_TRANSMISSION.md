# Connected rooms with a transmission estimate: fourth version

This version adds **frequency-dependent transmission loss through both doors and
the three separating walls**. It is a new editor and script; all three earlier
versions and their configurations are unchanged. No additional packages are needed.

PDFs now use [lightweight rendering defaults](README_REPORTS.md): fewer waterfalls,
bounded display points, rasterized surfaces and one diffuse decay map per room.
Full calculation data is retained.

Double-click **Open Transmission Editor.command**, or run:

```bash
cd room-acoustics
.venv/bin/python room_gui_transmission.py
```

The example starts with **both doors closed** and speakers in Room A. The diffuse
model predicts nonzero leakage into Room B and the corridor. Its default loss
curves are **illustrative inputs**, not measured properties of your construction.

## Enter transmission loss

In the **Transmission** tab, edit the transmission-loss table in dB at 63, 125,
250, 500, 1000, 2000 and 4000 Hz. Scroll horizontally to reach the higher bands.
There is a separate row for:

- Door A between Room A and the corridor.
- Door B between Room B and the corridor.
- The wall separating Room A from Room B.
- The remaining wall between Room A and the corridor, excluding Door A.
- The remaining wall between Room B and the corridor, excluding Door B.

**Higher transmission loss means less transmitted energy.** Enter `blocked` to
disable transmission through a partition at one band. When a door is open, its
effective TL is 0 dB, irrespective of its saved closed-door curve. Closing the
door restores that curve. A door and its surrounding wall are parallel transmission
paths; door area is excluded from the wall area to avoid counting it twice.

Use measured **band-by-band transmission-loss data** for the complete wall or door
assembly when available. A single-number sound-isolation rating does not describe
the frequency curve. Wall thickness changes the floor plan but does not calculate
material TL automatically. This version has no structural panel/mass/stiffness model.

Absorption and transmission are separate inputs. For every closed partition, the
model checks `absorbed_fraction + transmitted_fraction <= 1`. Absorption values
should represent energy actually absorbed, not a combined absorption-plus-leakage
loss. Reduce absorption or raise TL if the GUI reports an invalid energy budget.

All room, door-state, speaker and listener controls remain available. **Create
report** saves a configuration snapshot and calculates the report in a background
process. Runs are stored in separate dated folders under `reports_transmission`.
The enabled checkbox can suppress the diffuse estimate while retaining the air-path
simulation. Load an existing connected-room configuration to add the new settings;
save it under a new filename to retain your old file.

## What is being modeled

The report contains **two distinct models with separate reference levels**:

1. **Air-path model:** pyroomacoustics calculates geometric direct/reflected paths
   within connected air spaces. Open doors connect them. Closed doors and walls
   block those paths. These pressure impulse responses are labeled *air-path only*.
2. **Diffuse transmission model:** a three-zone energy balance estimates room-average
   received levels and decay, including transfer through closed doors and walls.
   It uses the supplied TL curves and the areas of each interface. Frequency-band
   energy coupling is a standard modeling approach for communicating rooms;
   see [room coupling](https://doc.comsol.com/6.4/doc/com.comsol.help.aco/aco_ug_geometrical_aco.11.66.html).

For each band, the transmitted energy fraction is `tau = 10**(-TL/10)`. The net
power transferred between two zones is modeled as:

```text
P_ij = c × interface_area × tau × (E_i / V_i − E_j / V_j) / 4
```

Here `E` is total acoustic energy, `V` is zone volume, and `c` is sound speed.
Transmission redistributes energy between rooms; absorption removes it. The solver
finds the steady state, then computes decay after all sources switch off. It checks
that the system decays rather than generating or indefinitely accumulating energy.
This is a lumped, Sabine-type diffuse approximation, not a spatial diffusion or
structural finite-element solver.

The diffuse model assumes equal relative emitted source power in each supplied
band, weighted by each speaker's `gain_db`. Sources add **incoherently as energy**.
Source delay settings do not affect these steady-state results. After switch-off,
all sources are assumed to stop simultaneously. The geometric model retains the
configured delays and its coherent speaker-sum channel.

**The models are not added together.** Their normalizations and assumptions differ;
the air-path baseline retains nontransmitting geometric boundaries. The code does
not fabricate a transmitted pressure waveform or mix a statistical level into the
pressure impulse responses. It exports the two datasets separately.

## Practical limits

- Diffuse estimates are **room averages**: two listeners in the same space receive
  the same result even if their exact positions differ. Within-room placement still
  affects the separate geometric response.
- This model provides no transmitted pressure phase, propagation delay, direct
  transmitted path, or exact low-frequency/modal response. Diffuse-field approaches
  are mainly useful above the modal region and after sufficient mixing.
  [Diffuse-field theory and validity](https://doc.comsol.com/6.4/doc/com.comsol.help.aco/aco_ug_geometrical_aco.11.75.html)
- Narrow bass resonances, diffraction, structural vibration, flanking paths,
  outdoor leakage, and explicit seal/gap geometry are not simulated.
- Strongly absorbing rooms and large open connections weaken the diffuse-zone
  assumptions. Doorway reveal volumes and areas are neglected in this reduced model.
- All plotted levels are **relative**, not calibrated dB SPL or certified isolation
  ratings. The supplied TL curves control transmission; the script does not infer
  construction quality from wall thickness or appearance.

## Report and exported data

The PDF starts with the floor plan and model explanation, then shows the diffuse
received-level comparison, effective TL curves, and per-listener energy-decay maps.
The original-style geometric response and waterfall pages follow with explicit
**air-path** labels.

Files include:

- `transmission_comparison.png`: band levels and effective TL curves.
- `transmission_decay_listener_XX.png`: band energy after sources switch off.
- `transmission_levels.csv`: individual-source and incoherent-total levels.
- `transmission_data.npz`: energy densities, decay, matrix coefficients and labels.
- `air_path_simulation_data.npz` and `air_path_frequency_response.csv`: separate
  geometric results, with no transmitted pressure component.
- `run_summary.json`: both methods, assumptions, effective transmission fractions,
  interface areas, room-average levels, and the captured configuration.

The main transmission array axes are:

| Array | Axes |
| --- | --- |
| `listener_source_density` | listener × speaker × frequency band |
| `listener_total_density` / `listener_total_relative_db` | listener × frequency band |
| `listener_decay_density` / `listener_decay_relative_db` | listener × time × frequency band |
| `exchange_matrices` | frequency band × receiving zone × emitting zone |

The three zones are ordered Room A, Room B, corridor. Energy density uses arbitrary
relative source-power normalization. Relative dB uses `10*log10(density/reference)`;
the exported reference is shared across rooms and bands.

Run without a GUI:

```bash
.venv/bin/python simulate_transmission.py --config room_config_transmission.json --output output_transmission
```

For custom bands, edit `transmission.center_freqs_hz` and provide equally long
`loss_db` lists in JSON. A scalar applies the same TL to all bands, and `null`
blocks that path. The GUI expands scalars when loading and adapts its column headers.

Check conservation, reciprocity, TL sensitivity, GUI editing, and the report button:

```bash
ROOM_GUI_TEST=1 QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -p test_transmission.py -v
```
