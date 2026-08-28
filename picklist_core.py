"""Core parsing, picklist, and mixing logic for the Echo picklist app."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


PICKLIST_COLUMNS = [
    "Source Plate Name",
    "Source Plate Type",
    "Source Well",
    "Sample Comments",
    "Destination Plate Name",
    "Destination Well",
    "Transfer Volume",
]


@dataclass(frozen=True)
class ReplacementSelection:
    csv_path: Path
    selected_names: Sequence[str]
    plate_name: str


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _read_rows(path: Path) -> List[List[str]]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError("CSV file not found: {}".format(path))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.reader(handle)]


def _plate_name(rows: Sequence[Sequence[str]], fallback: str) -> str:
    for row in rows[:5]:
        joined = " ".join(_clean(cell) for cell in row if _clean(cell))
        if joined.startswith("Plate "):
            return joined[len("Plate ") :].strip() or fallback
    return fallback


def _header_index(rows: Sequence[Sequence[str]], required: Iterable[str]) -> int:
    wanted = {item.upper() for item in required}
    for index, row in enumerate(rows):
        values = {_clean(cell).upper() for cell in row}
        if wanted.issubset(values):
            return index
    raise ValueError("Could not find a CSV header containing {}.".format(", ".join(required)))


def _dict_rows(rows: Sequence[Sequence[str]], header_index: int) -> List[Dict[str, str]]:
    header = [_clean(value) for value in rows[header_index]]
    result: List[Dict[str, str]] = []
    for source_row in rows[header_index + 1 :]:
        padded = list(source_row) + [""] * max(0, len(header) - len(source_row))
        item = {header[i]: _clean(padded[i]) for i in range(len(header)) if header[i]}
        if any(item.values()):
            result.append(item)
    return result


def _find_column(columns: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    by_lower = {_clean(column).lower(): column for column in columns}
    for candidate in candidates:
        if candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    return None


def parse_source1(path: Path) -> Tuple[List[Dict[str, str]], str]:
    """Parse the base plate, preserving logical replacement keys for relocated staples."""
    rows = _read_rows(Path(path))
    header_index = _header_index(rows, ("WELL POSITION", "SEQUENCE NAME"))
    records = _dict_rows(rows, header_index)
    if not records:
        raise ValueError("Base source contains no data rows.")
    columns = list(records[0])
    well_column = _find_column(columns, ("Well Position", "Well"))
    sequence_column = _find_column(
        columns, ("Sequence with spaces", "Sequence (5' > 3')", "Sequence")
    )
    original_well_column = _find_column(
        columns, ("Original Well Position", "Original Well", "Replacement Key")
    )
    if not well_column or not sequence_column:
        raise ValueError("Base source must contain well and sequence columns.")
    by_replacement_key: Dict[str, Dict[str, str]] = {}
    key_order: List[str] = []
    for row in records:
        well = row.get(well_column, "").upper()
        if not well:
            continue
        original_well = row.get(original_well_column or "", "").upper()
        replacement_key = original_well or well
        if replacement_key not in by_replacement_key:
            key_order.append(replacement_key)
        by_replacement_key[replacement_key] = {
            "Well": well,
            "Replacement Well": replacement_key,
            "Sequence": row.get(sequence_column, ""),
        }
    return [by_replacement_key[key] for key in key_order], _plate_name(rows, "SourcePlate1[1]")


def parse_source2(path: Path) -> Tuple[List[Dict[str, str]], str]:
    """Parse a replacement plate while keeping source and replacement wells distinct."""
    rows = _read_rows(Path(path))
    header_index = _header_index(rows, ("WELL", "NAME"))
    records = _dict_rows(rows, header_index)
    if not records:
        raise ValueError("Replacement source contains no data rows: {}".format(path))
    columns = list(records[0])
    well_column = _find_column(columns, ("Well", "Well Position", "Source Well"))
    name_column = _find_column(columns, ("Name", "Sequence Name"))
    sequence_column = _find_column(
        columns, ("Sequence", "Sequence with spaces", "Sequence (5' > 3')")
    )
    replacement_column = _find_column(
        columns, ("Replace Well", "Replacement Well", "replace_well", "replacewell")
    )
    if not all((well_column, name_column, sequence_column, replacement_column)):
        raise ValueError(
            "Replacement CSV must contain Well, Name, Sequence, and Replace Well columns: {}".format(path)
        )
    parsed = []
    for row in records:
        well = row.get(well_column or "", "").upper()
        name = row.get(name_column or "", "")
        target = row.get(replacement_column or "", "").upper()
        if well and name and target:
            parsed.append(
                {
                    "Well": well,
                    "Name": name,
                    "Sequence": row.get(sequence_column or "", ""),
                    "Replace Well": target,
                }
            )
    if not parsed:
        raise ValueError("Replacement source has no complete replacement rows: {}".format(path))
    return parsed, _plate_name(rows, "SourcePlate2[1]")


def replacement_names(path: Path) -> set[str]:
    rows, _ = parse_source2(path)
    return {row["Name"] for row in rows}


def _selected_rows(rows: Sequence[Dict[str, str]], names: Sequence[str]) -> List[Dict[str, str]]:
    requested = {_clean(name) for name in names if _clean(name)}
    by_name = {row["Name"]: row for row in rows}
    missing = sorted(requested - set(by_name))
    if missing:
        raise ValueError("Selected replacement(s) not found in CSV: {}".format(", ".join(missing)))
    return [by_name[name] for name in names if name in by_name]


def generate_picklist(
    base_source_path: Path,
    replacements: Sequence[ReplacementSelection],
    destination_wells: Sequence[str],
    source_plate_type: str = "384PP_AQ_BP",
    destination_plate_name: str = "Destination[1]",
    transfer_volume_nl: int = 50,
    max_destination_volume_ul: float = 12.5,
    transfers_per_source: int = 1,
) -> List[Dict[str, object]]:
    """Apply replacements and build Echo-format transfer rows."""
    destinations = [_clean(well).upper() for well in destination_wells if _clean(well)]
    if not destinations:
        raise ValueError("Enter at least one destination well.")
    if transfer_volume_nl <= 0 or max_destination_volume_ul <= 0 or transfers_per_source <= 0:
        raise ValueError("Transfer volume, destination capacity, and transfers/source must be positive.")

    base_rows, base_plate_name = parse_source1(Path(base_source_path))
    selected_replacements: List[Dict[str, str]] = []
    targets: Dict[str, Tuple[Path, str]] = {}

    for selection in replacements:
        replacement_rows, file_plate_name = parse_source2(Path(selection.csv_path))
        plate_name = _clean(selection.plate_name) or file_plate_name
        for row in _selected_rows(replacement_rows, selection.selected_names):
            target = row["Replace Well"].upper()
            if target in targets:
                prior_path, prior_name = targets[target]
                raise ValueError(
                    "Duplicate replacement for {}: '{}' in {} conflicts with '{}' in {}.".format(
                        target, row["Name"], selection.csv_path, prior_name, prior_path
                    )
                )
            targets[target] = (Path(selection.csv_path), row["Name"])
            selected_replacements.append(
                {
                    "Well": row["Well"],
                    "Sequence": row["Sequence"],
                    "Source Plate Name": plate_name,
                }
            )

    active = [
        {"Well": row["Well"], "Sequence": row["Sequence"], "Source Plate Name": base_plate_name}
        for row in base_rows
        if row.get("Replacement Well", row["Well"]).upper() not in targets
    ]
    active.extend(selected_replacements)

    transfers: List[Dict[str, object]] = []
    for source in active:
        for _ in range(transfers_per_source):
            transfers.append(
                {
                    "Source Plate Name": source["Source Plate Name"],
                    "Source Plate Type": source_plate_type,
                    "Source Well": source["Well"].upper(),
                    "Sample Comments": source["Sequence"],
                }
            )

    capacity = int(max_destination_volume_ul * 1000) // transfer_volume_nl
    if capacity < 1:
        raise ValueError("Transfer volume is larger than the destination-well capacity.")
    total_capacity = capacity * len(destinations)
    if len(transfers) > total_capacity:
        raise ValueError(
            "Too many transfers ({}) for the destination wells (maximum {}).".format(
                len(transfers), total_capacity
            )
        )
    for index, transfer in enumerate(transfers):
        transfer["Destination Plate Name"] = destination_plate_name
        transfer["Destination Well"] = destinations[index // capacity]
        transfer["Transfer Volume"] = transfer_volume_nl
    return transfers


def parse_vector(text: str, length: int, label: str) -> List[float]:
    try:
        values = [float(value.strip()) for value in text.split(",")]
    except ValueError as exc:
        raise ValueError("{} must contain {} comma-separated numbers.".format(label, length)) from exc
    if len(values) != length:
        raise ValueError("{} must contain exactly {} numbers.".format(label, length))
    return values


def calculate_mixing_volumes(
    staple: Sequence[float],
    number_of_staples: int,
    scaffold: Sequence[float],
    magnesium: Sequence[float],
    sodium: Sequence[float],
    te_10x: Sequence[float],
    desired: Sequence[float],
) -> List[Dict[str, float]]:
    """Calculate reagent volumes, with every reagent included in the water balance."""
    try:
        final_volume = desired[0]
        staple_volume = (final_volume / (staple[0] / desired[1])) * number_of_staples
        scaffold_volume = final_volume / (scaffold[0] / desired[2])
        te_volume = (
            final_volume - scaffold_volume * scaffold[1] - staple_volume * staple[1]
        ) / (te_10x[1] / desired[3])
        magnesium_volume = final_volume / (magnesium[2] / desired[4])
        sodium_volume = final_volume / (sodium[3] / desired[5])
    except (IndexError, ZeroDivisionError) as exc:
        raise ValueError("Mixing concentrations and desired values must be non-zero where used.") from exc
    water_volume = final_volume - sum(
        (staple_volume, scaffold_volume, te_volume, magnesium_volume, sodium_volume)
    )
    values = [staple_volume, scaffold_volume, te_volume, magnesium_volume, sodium_volume, water_volume]
    if any(value < -1e-9 for value in values):
        raise ValueError("Mixing parameters produce a negative reagent volume.")
    labels = ["Staple mix (from picklist)", "Scaffold", "10X TE", "Mg (100 mM)", "Na (100 mM)", "DI Water"]
    return [{"Reagent": label, "Volume_uL": value} for label, value in zip(labels, values)]


def combine_picklists(
    picklists: Sequence[Sequence[Dict[str, object]]],
) -> List[Dict[str, object]]:
    """Combine stored picklists while preventing two designs from sharing a destination well."""
    if not picklists:
        raise ValueError("Select at least one stored picklist.")

    combined: List[Dict[str, object]] = []
    occupied_by: Dict[Tuple[str, str], int] = {}
    for picklist_index, rows in enumerate(picklists, 1):
        if not rows:
            raise ValueError("Stored picklist {} contains no transfers.".format(picklist_index))
        occupied = {
            (_clean(row.get("Destination Plate Name")), _clean(row.get("Destination Well")).upper())
            for row in rows
        }
        for destination in occupied:
            if destination in occupied_by:
                raise ValueError(
                    "Stored picklists {} and {} both use destination {} / {}.".format(
                        occupied_by[destination], picklist_index, destination[0], destination[1]
                    )
                )
            occupied_by[destination] = picklist_index
        combined.extend(dict(row) for row in rows)
    return combined


def separate_mixing_recipes(
    recipes: Sequence[Sequence[Dict[str, object]]],
) -> List[List[Dict[str, object]]]:
    """Validate and preserve one independent mixing recipe for each stored run."""
    if not recipes:
        raise ValueError("Select at least one stored mixing recipe.")

    separated: List[List[Dict[str, object]]] = []
    for recipe_index, rows in enumerate(recipes, 1):
        if not rows:
            raise ValueError("Stored mixing recipe {} contains no rows.".format(recipe_index))
        recipe: List[Dict[str, object]] = []
        for row in rows:
            reagent = _clean(row.get("Reagent"))
            if not reagent:
                raise ValueError("Stored mixing recipe {} has a row without a reagent name.".format(recipe_index))
            try:
                volume = float(row.get("Volume_uL", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("Stored volume for '{}' is not numeric.".format(reagent)) from exc
            recipe.append({"Reagent": reagent, "Volume_uL": volume})
        separated.append(recipe)
    return separated


def write_csv(path: Path, rows: Sequence[Dict[str, object]], columns: Sequence[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def valid_well_list(text: str) -> List[str]:
    wells = [_clean(value).upper() for value in text.split(",") if _clean(value)]
    invalid = [well for well in wells if not re.fullmatch(r"[A-P](?:0[1-9]|1[0-9]|2[0-4])", well)]
    if invalid:
        raise ValueError("Invalid 384-well position(s): {}".format(", ".join(invalid)))
    return wells
