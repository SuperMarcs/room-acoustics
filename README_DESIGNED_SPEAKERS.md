# Speaker Studio → room simulation

This new room version connects the Julia speaker designer to the Python multi-listener room simulation. Original room, multi-listener, connected-room, transmission and subwoofer scripts are unchanged.

## 1. Export from Speaker Studio

Restart Speaker Studio so the new **Export to room** button appears. Design the speaker and apply any pending changes, then click that button. It creates a folder under `../speaker-studio-julia/room_exports/` containing:

- **speaker_room_profile.json** — the actual complex acoustic response for importing into the room.
- **frequency_response.svg** and **report.html** — graphs of the exported on-axis response at 1 m.
- **response.csv**, **design.toml**, excursion plot and any measured FRD inputs.

The export always uses the cabinet's on-axis response at a 1 m reference distance, at the designer's current input voltage. It is independent of the designer's listening distance/angles. The common 1 m propagation phase is removed; crossover phase, driver relative phase, polarity and delay remain. This prevents adding the designer's listening distance a second time in the room.

If enabled measured drivers have limited frequency ranges, the export is restricted to their shared valid range. It does not extrapolate missing measurements. Response samples are spaced at most 1 Hz; graph files remain compact.

If running inside Julia's `julia>` REPL, change to the `speaker-studio-julia` project folder and start the designer with:

```julia
include("speaker_gui.jl")
SpeakerStudio.main()
```

## 2. Open the new room GUI

Double-click **Open Designed Speaker Room Editor.command**, or use a normal shell terminal:

```sh
cd room-acoustics
.venv/bin/python room_gui_designed_speakers.py
```

This room GUI is Python; the commands above are shell commands rather than Julia REPL expressions. It reuses the existing `room-acoustics/.venv`, with no new dependencies.

File selection uses Qt's own file browser, including speaker import, room load/save and report-folder selection. The browser opens asynchronously so the main Qt event loop keeps running. This avoids the native macOS file panels when using TaskRecorder. Close and reopen the editor after updating to use the new dialogs.

To import without any pop-up, paste the full path to `speaker_room_profile.json` into the field beneath **Load speaker design…**, then click **Load path** or press Enter. This provides a fallback while recording; enclosing quotes and `~` home paths are accepted.

## 3. Assign the speaker design

1. Select a room speaker, such as **Left**.
2. Click **Load speaker design…** and select **speaker_room_profile.json** from the exported folder. Import the JSON response data; the SVG/PNG graph is for viewing.
3. The **Speaker graph** tab previews the imported 1 m response. Return to **Room plan** to place the speaker and listeners.
4. Select **Right** and load the same export for a matching pair, or a different export for another design. Every room speaker has an independent assignment. **Use flat reference** clears the selected assignment.
5. Configure the room, wall absorption, source/listener positions, gains and delays, then press **Create report**.

The speaker position in the room is the equivalent cabinet reference point (the baffle center), not its bottom corner. The room editor supports multiple speakers and multiple listening positions.

## Reports and saved layouts

Reports go into `reports_designed_speakers/` by default and include a compact PDF, top view, imported design graphs, listening-position comparison, per-listener speaker + room frequency responses, CSV and NPZ data. The dashed gray comparison uses flat sources in the same room so you can see how the speaker design changes the response. This version has **no waterfalls**.

Speaker profiles are embedded into room configuration JSON files and deduplicated when the same design is assigned to both speakers. Saved layouts are portable and do not depend on the original export folder. Report jobs capture the configuration and responses when **Create report** is clicked; later edits do not change the running job. After changing the design in Julia, export it again and reload the new JSON into the room GUI.

Command-line report generation:

```sh
.venv/bin/python simulate_room_designed_speakers.py \
  --config room_config_designed_speakers.json \
  --output output_designed_speakers
```

The supplied default configuration uses flat references until you assign exports.

## Calculation and limits

For source `i` and listener `j`, the calculation uses:

```text
H_combined[j,i,f] = 4π × H_room[j,i,f] × P_speaker[i,f at 1 m]
H_total[j,f] = sum_i H_combined[j,i,f]
```

The 4π converts pyroomacoustics' point-source Green function `1/(4πr)` to the profile's pressure-at-1-m convention. Configured room source gain/delay are already included in `H_room`. Multiplication uses the complex response, so cancellation and phase are retained; each speaker is filtered before coherent summation. Unknown frequencies are left blank. Dense Cartesian cubic interpolation avoids phase-wrap interpolation.

Relative graphs use a single **flat-source room peak** reference across all listeners/channels, with the same reference for the dashed comparison. Additional predicted SPL columns appear in CSV/NPZ; these are model predictions based on the speaker's exported RMS voltage, not calibrated measurements. An unassigned flat source is 1 Pa at 1 m (about 94 dB SPL). Room gain applies in addition to the exported voltage.

**The cabinet is an equivalent omnidirectional point source.** Its fixed on-axis response is applied to direct sound and reflections alike. This does not transfer the full angular radiation pattern, toe-in, cabinet scattering or separate driver geometry into the room. It is suitable for exploratory enclosure/crossover + room comparisons, with the limitations of the underlying T/S or measured-response model.

The report is a **frequency-domain calculation**. `simulation_data.npz` contains `transfer_function` (speaker + room complex pressure), `speaker_response_pa_at_1m`, relative levels and predicted SPL. `room_rir` / `room_transfer_function` are explicitly the raw flat-source room responses. They are not speaker-filtered impulse responses. This version does not generate a speaker-filtered audio signal or infer a waterfall from those raw impulses.

The new default uses a 48 kHz room sample rate and a 20 Hz–20 kHz report range, while keeping plotted curves bounded to 1,200 display points. Existing room scripts/configurations keep their original settings. Report frequencies must remain below Nyquist and inside the speaker export's supported band. Material absorption, room truncation and the point-source approximation still limit accuracy. See [pyroomacoustics room simulation documentation](https://pyroomacoustics.readthedocs.io/en/pypi-release/pyroomacoustics.room.html).

## Verification

From the room-acoustics folder:

```sh
MPLBACKEND=Agg .venv/bin/python -m unittest discover -s tests -p test_designed_speakers.py -v
```

To include the Qt workflow, add `ROOM_GUI_TEST=1`. To check a real Julia export against its reference graph, set `SPEAKER_ROOM_EXPORT` to the exported JSON path. Native GUI checks need a desktop session.
