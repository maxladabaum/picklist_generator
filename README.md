# Echo Picklist Generator

A standalone desktop version of the GUI and picklist workflow from
`picklist_generator_replacement-Copy1.ipynb`. Yaritza Extensions, Aptamers,
MB, PAINT P1, PAINT R1, and the base source sheet are loaded
automatically with portable, relative paths. The
original Set B CSV remains archived in `replacement_sheets/` but is not loaded
or shown by the application.

## Start the app

Python 3.9 or newer is required. A standard Python installer from
[python.org](https://www.python.org/downloads/) includes Tkinter, which the app
uses for its desktop interface.

- macOS: double-click `launch.command`. If macOS blocks the first launch,
  Control-click it, choose **Open**, then confirm.
- Windows: double-click `launch.bat`.
- Linux: run `./launch.sh`. If Tkinter is not installed, install your
  distribution's `python3-tk` package first.
- Terminal: run `python3 picklist_app.py` from this folder.

The launchers create a local `.venv` on first use. There are no third-party
Python packages to download. Generated files default to `generated_output/`.
Every successful generation creates a new date-and-time folder, for example
`generated_output/2026-08-10_14-32-07/`, containing that run's picklist and
mixing recipe. A numeric suffix is added if two runs occur in the same second.

On first launch, the app creates two local JSON files automatically from the
bundled `.example.json` defaults:

- `config.json` stores the most recent output paths, the wells used by the last
  successful run, the pending wells for the next run, output root, destination
  plate name, transfer settings, and last successful run.
- `destination_plate_state.json` records transfer count, volume, and last-use
  time for every destination well used by a successful generation.
- `storage_state.json` stores each named run's picklist, mixing-recipe rows, and
  replacement-panel selection metadata in the persistent Storage queue.

At startup, the most recent output paths are restored. The destination-well
field defaults to the first unused wells on the current destination plate,
using the same number of wells as the most recent entry.

Immediately after successful generation, the destination plate state records
the transfer count and volume delivered to each used well. The destination-well
field then advances to the next unused wells, and both the completed and pending
well lists are saved in `config.json`.

Use **Destination starting well** in Run & Mixing Settings to override where
automatic destination allocation begins. For example, choosing C01 skips open
wells before C01 and fills from unused wells at or after C01. A starting well
already recorded as used is rejected. The override does not change normal
unused-well behavior on the next launch. Use **Start fresh plate** on the
Destination Plate tab to clear all recorded usage for the currently named
plate after confirmation. Existing output folders are retained.

## Workflow

1. Open **Replacements** and select wells in Yaritza Extensions, Aptamers, MB,
   PAINT P1, or PAINT R1. Grey wells do not have a corresponding
   row in the selected replacement CSV.
2. Click **Check selections** at the bottom of the Replacements tab. Any
   selections targeting the same base well turn red with an × and are listed
   in the compatibility box. Use **Save selections PDF** to export one PDF
   report containing selected replacements from every used panel, including
   panels that are not currently visible. The PDF is saved automatically in a
   new timestamped folder beneath the configured Generated output root.
3. Open **Run & Mixing Settings**. Confirm destination wells, volumes, plate
   names, and mixing inputs.
4. Click **Generate Preview**. This calculates the picklist and mixing recipe
   without writing files, recording destination usage, or advancing wells.
5. Open **Destination Plate** to see the 16×24 plate map. Previously used wells,
   wells at capacity, and the wells currently entered in Settings have distinct
   colors. Click any well for its recorded transfer and volume details.
6. Review the previews under **Results**. Choose **Save picklist + recipe** to
   write the individual CSV files, or choose **Send to storage** and enter a run
   name without writing those individual files. Either action records the
   destination usage and advances the wells once. You can subsequently use the
   other action on the same preview without recording the transfers twice.
7. In **Storage**, select individual runs (Command/Ctrl-click for multiple), or
   click **Select all**, then click **Generate combined picklist + recipe**. The
   transfer rows are concatenated into one large picklist, while each run keeps
   its own mixing-recipe CSV with the run name in its filename. Volumes are not
   summed across runs. Everything is written to a new timestamped output folder.
   Runs that reuse the same destination plate and well cannot be combined.
8. Use **Generate selections PDF** in Storage to create one large PDF for the
   selected runs. Every named run starts on a new page and includes all of its
   selected replacement panels. **Select all** includes every stored run. The
   PDF is saved automatically in a new timestamped generated-output folder.
9. Individual CSV paths are shown in Settings; after a combined generation,
   the Storage tab reports its new output folder and a confirmation shows the
   full path.

Each replacement row identifies a base `Replace Well`. The generator removes
that base well and substitutes the replacement sheet's source well. Selecting
two replacements for the same base well is rejected. Transfers are assigned to
destination wells in order, up to the configured volume capacity.

The mixing fields retain the notebook's comma-separated vector format. The
water balance fixes a notebook arithmetic omission by subtracting sodium along
with the other reagent volumes, so the recipe sums to the requested final
volume.

## Repository contents

- `picklist_app.py` — Tkinter desktop interface
- `picklist_core.py` — CSV parsing, replacement, transfer, and mixing logic
- `selection_report.py` — dependency-free PDF selection-report renderer
- `app_state.py` — persistent configuration and destination usage helpers
- `config.example.json` — clean first-run configuration defaults
- `destination_plate_state.example.json` — clean, unused plate-state example
- `storage_state.example.json` — clean persistent-storage example
- `config.json` — local user defaults and recent-run metadata, created at runtime
- `destination_plate_state.json` — local 384-well state, created at runtime
- `storage_state.json` — local stored-run queue, created at runtime
- `replacement_sheets/` — all bundled source/replacement CSV files
- `launch.command` / `launch.bat` / `launch.sh` — platform launchers
- `tests/` — logic regression tests

The `.venv/`, generated CSVs, `config.json`, `destination_plate_state.json`,
and `storage_state.json` are intentionally ignored by Git. This keeps
each user's paths and plate history private. Commit the example JSON files so a
fresh clone always starts with an unused destination plate.
