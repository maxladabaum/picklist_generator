# Echo Picklist Generator

A standalone desktop version of the GUI and picklist workflow from
`picklist_generator_replacement-Copy1.ipynb`. Yaritza Extensions, Aptamers,
MB, PAINT P1, PAINT R1, and the base source sheet are loaded
automatically with portable, relative paths. The
original Set B CSV remains archived in `replacement_sheets/` but is not loaded
or shown by the application.

## Start the app

**Soyeon MB** in the Replacements tab includes 60 D-MB extensions (rows H03,
H05, H07, H09, and H11) and a separate panel for the six DL extensions supplied
in `DNA Book Sub-group (Myoungseok) - Soyeon MB.csv`. The bundled normalized
sheet is `replacement_sheets/soyeon_MB_replace.csv`; original names, sequences,
and source wells are preserved. Its source plate is labeled `SoyeonMB[1]` in
the picklist. Targets B21, B22, and B20 are translated to logical replacement
keys B11, B14, and L14, respectively, to account for relocated base staples.
These selections participate in the existing clash checks, previews, and storage.

In **Replacements**, choose **Hinge type**: **Flexible hinges (original
wells)** (the default) or **Rigid center connectors (F19–H24)**. Each option includes 18 hinges and
excludes the other set. The choice is remembered and recorded with stored runs.
The base CSV uses `Hinge Type` and `Original Well Position` to associate the
alternatives with the same replacement sites, so replacements work with either
hinge type. Staple counts and mixing volumes use only the selected set.

The source plate map also shows six no-center-connector alternatives at
J19–J24, targeting J16, I16, C02, D02, L16, and K16 respectively. These source
wells and sequences are absent from the bundled `book_base.csv`, so this
option is not yet available in the picker.

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
2. To generate an identification template from a replacement design, open its
   panel and click **Make template from this panel**. This transfers the exact
   selected-position pattern to **Origami Templates**. The editor starts with
   no predefined bit groups. Select any physical sites, enter a name, and create
   either a digital-bit group or an always-on alignment group. Repeat this for
   any encoding scheme; a digital bit may contain any number or arrangement of
   physical sites, so one missing extension need not become a complete bit
   error. Adjacent replacement
   columns use a uniform 10.909 nm pitch and adjacent displayed rows use a
   uniform 5.0 nm pitch. This makes the 12×8 outer-site span 120×35 nm.
   Up-extension panels use the mirrored physical
   orientation (left-to-right columns 12 through 1), while down-extension
   panels use columns 1 through 12. Both the interactive replacement sheets
   and their exported PDF reports use this physical pitch. The default 8×12
   grid and 20 nm image margin therefore produce a 160×80 nm
   raster footprint. Choose which digital groups are ON, then click **Save
   template PNG**. The PNG colors every rendered group differently for visual
   inspection; Paint Analysis removes those annotation colors before alignment.
   Upload the resulting bright-on-dark PNG in the paint-analysis Origami identification panel. The
   PNG embeds its x/y nanometres-per-pixel calibration, and a same-name JSON
   sidecar also records the physical dimensions, alignment strokes, logical-bit
   definitions and states, and expanded physical site list; it is documentation
   and does not need to be uploaded because the same metadata is embedded in the PNG.
3. Click **Check selections** at the bottom of the Replacements tab. Any
   selections targeting the same base well turn red with an × and are listed
   in the compatibility box. Use **Save selections PDF** to export one PDF
   report containing selected replacements from every used panel, including
   panels that are not currently visible. The PDF is saved automatically in a
   new timestamped folder beneath the configured Generated output root.
4. Open **Run & Mixing Settings**. Confirm destination wells, volumes, plate
   names, and mixing inputs.
5. Click **Generate Preview**. This calculates the picklist and mixing recipe
   without writing files, recording destination usage, or advancing wells.
6. Open **Destination Plate** to see the 16×24 plate map. Previously used wells,
   wells at capacity, and the wells currently entered in Settings have distinct
   colors. Click any well for its recorded transfer and volume details.
7. Review the previews under **Results**. Choose **Save picklist + recipe** to
   write the individual CSV files, or choose **Send to storage** and enter a run
   name without writing those individual files. Either action records the
   destination usage and advances the wells once. You can subsequently use the
   other action on the same preview without recording the transfers twice.
8. In **Storage**, select individual runs (Command/Ctrl-click for multiple), or
   click **Select all**, then click **Generate combined picklist + recipe**. The
   transfer rows are concatenated into one large picklist, while each run keeps
   its own mixing-recipe CSV with the run name in its filename. Volumes are not
   summed across runs. Everything is written to a new timestamped output folder.
   Runs that reuse the same destination plate and well cannot be combined.
9. Use **Generate selections PDF** in Storage to create one large PDF for the
   selected runs. Every named run starts on a new page and includes all of its
   selected replacement panels. **Select all** includes every stored run. The
   PDF is saved automatically in a new timestamped generated-output folder.
10. Individual CSV paths are shown in Settings; after a combined generation,
   the Storage tab reports its new output folder and a confirmation shows the
    full path.

### Custom logical-bit encodings

The Origami Templates tab includes a visual group editor plus **Load bit schema
JSON** and **Save bit schema JSON**. There are no default groups. A schema may
use any physical grid dimensions, number of bits, bit names, and site
groupings. Physical coordinates are one-based `[row, column]` pairs.

```json
{
  "format": "paint-analysis-logical-bits-v1",
  "physical_rows": 4,
  "physical_columns": 6,
  "alignment_groups": [
    {"id": "anchor", "physical_sites": [[1, 1], [1, 2], [2, 1]]}
  ],
  "logical_bits": [
    {
      "id": "bit_a",
      "label": "Bit A",
      "physical_sites": [[2, 3], [2, 4], [2, 5]],
      "evidence_sites": [[2, 3], [2, 4], [2, 5]]
    }
  ]
}
```

`physical_sites` controls what is rendered when a bit is on.
`evidence_sites` controls which of those sites Paint Analysis aggregates when
measuring the bit and may omit ambiguous intersections. If omitted, it defaults
to `physical_sites`. Alignment groups are always rendered and never classified.
Physical sites may belong to more than one group. Their PNG colors blend, and
their physical state follows union/OR semantics: the site is bright when any
active group contains it. Paint Analysis does not use a shared bright site as
negative evidence against an overlapping group that is OFF.
Selecting a group in the table loads its sites into the physical-grid editor.
Use **Toggle bit ON/OFF** to choose whether a digital group is present in the
current template; alignment groups are always present. Saved template PNGs add
`active_logical_bits` and stable per-group display colors to the embedded copy
of the schema.

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
- `template_generator.py` — dependency-free grayscale PNG barcode-template renderer
- `logical_bit_schema.example.json` — editable arbitrary logical-bit schema example
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
