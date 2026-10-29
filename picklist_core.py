"""Core parsing, picklist, and mixing logic for the Echo picklist app."""

from __future__ import annotations

import csv
import math
from itertools import combinations
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

HINGE_TYPES = {
    "original": "Flexible hinges (original wells)",
    "balanced_rigid": "Rigid center connectors (F19–H24)",
}


@dataclass(frozen=True)
class ReplacementSelection:
    csv_path: Path
    selected_names: Sequence[str]
    plate_name: str
    desired_concentration_nm: float = 50.0
    replacement_fraction: float = 1.0
    site_fractions: Optional[Dict[str, float]] = None
    concentration_nm: float = 200000.0
    site_concentrations_nm: Optional[Dict[str, float]] = None
    replacement_tab: str = ""
    panel_names: Optional[Dict[str, str]] = None


def desired_concentration_ratio(
    desired_concentration_nm: float, base_desired_concentration_nm: float,
) -> float:
    """Validate final targets and return their molar ratio."""
    for label, value in (("base staple", base_desired_concentration_nm),
                         ("replacement", desired_concentration_nm)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Desired {} concentration must be a finite positive number.".format(label))
    return desired_concentration_nm / base_desired_concentration_nm


def validate_transfer_limits(minimum_transfer_nl=25.0, transfer_increment_nl=25.0):
    if any(not math.isfinite(v) or v <= 0 for v in (minimum_transfer_nl, transfer_increment_nl)):
        raise ValueError("Minimum transfer and transfer increment must be finite positive volumes.")


def valid_transfer(volume, minimum_transfer_nl=25.0, transfer_increment_nl=25.0):
    """Zero omits a transfer; positive transfers meet both independent limits."""
    return math.isfinite(volume) and (abs(volume) < 1e-9 or (
        volume >= minimum_transfer_nl - 1e-9 and math.isclose(
            volume / transfer_increment_nl, round(volume / transfer_increment_nl), rel_tol=0, abs_tol=1e-9)))


def replacement_fractions(
    transfer_volume_nl: float, desired_concentration_nm: float = 50.0,
    base_desired_concentration_nm: float = 50.0, concentration_ratio: float = 1.0,
    minimum_transfer_nl: float = 25.0, transfer_increment_nl: float = 25.0,
) -> List[float]:
    """Return splits meeting the minimum and increment for both source stocks."""
    validate_transfer_limits(minimum_transfer_nl, transfer_increment_nl)
    if transfer_volume_nl <= 0 or not valid_transfer(transfer_volume_nl, minimum_transfer_nl, transfer_increment_nl):
        raise ValueError("Transfer volume must be at least {:g} nL and a multiple of {:g} nL.".format(minimum_transfer_nl, transfer_increment_nl))
    droplets = transfer_volume_nl * desired_concentration_ratio(
        desired_concentration_nm, base_desired_concentration_nm) / transfer_increment_nl
    if not math.isfinite(droplets) or droplets <= 0:
        raise ValueError("Target transfer volume must be finite and positive.")
    if not math.isfinite(concentration_ratio) or concentration_ratio <= 0:
        raise ValueError("Stock concentrations must be finite positive numbers.")
    if math.isclose(droplets, round(droplets), rel_tol=0, abs_tol=1e-9):
        count = round(droplets)
        candidates = [index / count for index in range(count + 1)] if count else [1.0]
    else:
        # The retained base portion must be whole droplets even when the
        # target is below the base concentration. A full replacement can still
        # be possible because its stock is more dilute.
        candidates = [1 - original / droplets for original in range(math.floor(droplets), -1, -1)]
    fractions = [fraction for fraction in candidates
                 if all(valid_transfer(v, minimum_transfer_nl, transfer_increment_nl) for v in (
                     droplets * (1-fraction) * transfer_increment_nl,
                     droplets * fraction * concentration_ratio * transfer_increment_nl))]
    if not fractions:
        raise ValueError(
            "No valid replacement split: the requested target requires {:g} nL at 0% replacement "
            "or {:g} nL at 100% replacement. Every nonzero transfer must be at least {:g} nL "
            "AND a multiple of {:g} nL. Adjust the final volume, site target, or stock concentration.".format(
                droplets * transfer_increment_nl, droplets * concentration_ratio * transfer_increment_nl,
                minimum_transfer_nl, transfer_increment_nl))
    return fractions


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


def parse_source1(path: Path, hinge_type: str = "original") -> Tuple[List[Dict[str, str]], str]:
    """Parse the base plate, preserving logical replacement keys for relocated staples."""
    if hinge_type not in HINGE_TYPES:
        raise ValueError("Unknown hinge type: {}".format(hinge_type))
    rows = _read_rows(Path(path))
    header_index = _header_index(rows, ("WELL POSITION", "SEQUENCE NAME"))
    records = _dict_rows(rows, header_index)
    if not records:
        raise ValueError("Base source contains no data rows.")
    available_hinges = {row.get("Hinge Type", "") for row in records} - {""}
    if available_hinges and hinge_type not in available_hinges:
        raise ValueError("This base sheet does not contain the selected hinge type. Choose {}.".format(
            ", ".join(HINGE_TYPES.get(key, key) for key in sorted(available_hinges))))
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
        # Inventory supplements are selected explicitly; their logical keys may
        # also have an unmodified staple in the base mixture.
        if row.get("Usage", "") == "Supplemental":
            continue
        row_hinge_type = row.get("Hinge Type", "")
        if row_hinge_type and row_hinge_type not in HINGE_TYPES:
            raise ValueError("Unknown hinge type in base CSV: {}".format(row_hinge_type))
        if row_hinge_type and row_hinge_type != hinge_type:
            continue
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
            "Hinge Type": row_hinge_type,
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
        columns, ("Replace Well", "Replacement Well", "replace_well", "replacewell", "Replace")
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
    hinge_type: str = "original",
    base_concentration_nm: float = 200000.0,
    base_desired_concentration_nm: float = 50.0,
    minimum_transfer_nl: float = 25.0,
    transfer_increment_nl: float = 25.0,
) -> List[Dict[str, object]]:
    """Apply replacements and build Echo-format transfer rows."""
    destinations = [_clean(well).upper() for well in destination_wells if _clean(well)]
    if not destinations:
        raise ValueError("Enter at least one destination well.")
    if transfer_volume_nl <= 0 or max_destination_volume_ul <= 0 or transfers_per_source <= 0:
        raise ValueError("Transfer volume, destination capacity, and transfers/source must be positive.")
    if not math.isfinite(base_concentration_nm) or base_concentration_nm <= 0:
        raise ValueError("Base stock concentration must be a finite positive number.")

    validate_transfer_limits(minimum_transfer_nl, transfer_increment_nl)
    if not valid_transfer(transfer_volume_nl, minimum_transfer_nl, transfer_increment_nl):
        raise ValueError("Base transfer violates the minimum transfer or increment.")
    base_rows, base_plate_name = parse_source1(Path(base_source_path), hinge_type)
    selected_replacements: List[Dict[str, object]] = []
    targets: Dict[str, Tuple[Path, str]] = {}

    retained_volumes: Dict[str, float] = {}
    for selection in replacements:
        if not math.isfinite(selection.concentration_nm) or selection.concentration_nm <= 0:
            raise ValueError("Replacement stock concentration must be a finite positive number: {}".format(selection.csv_path))
        concentration_ratio = base_concentration_nm / selection.concentration_nm
        desired_concentration_ratio(
            selection.desired_concentration_nm,
            base_desired_concentration_nm,
        )
        overrides = selection.site_fractions or {}
        concentrations = selection.site_concentrations_nm or {}
        unknown = (set(overrides) | set(concentrations)) - set(selection.selected_names)
        if unknown:
            raise ValueError("Per-site percentages reference unselected replacements: {}".format(", ".join(sorted(unknown))))
        replacement_rows, file_plate_name = parse_source2(Path(selection.csv_path))
        plate_name = _clean(selection.plate_name) or file_plate_name
        for row in _selected_rows(replacement_rows, selection.selected_names):
            total_volume = transfer_volume_nl * desired_concentration_ratio(
                concentrations.get(row["Name"], selection.desired_concentration_nm),
                base_desired_concentration_nm,
            )
            fraction = overrides.get(row["Name"], selection.replacement_fraction)
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                raise ValueError("Replacement percentage must be between 0% and 100%: {}".format(row["Name"]))
            replacement_volume = total_volume * fraction * concentration_ratio
            original_volume = total_volume * (1 - fraction)
            if any(not valid_transfer(volume, minimum_transfer_nl, transfer_increment_nl)
                   for volume in (replacement_volume, original_volume)):
                raise ValueError("Replacement target and percentage must produce transfers with minimum {:g} nL and {:g} nL increments: {}.".format(minimum_transfer_nl, transfer_increment_nl, row["Name"]))
            replacement_volume = round(replacement_volume / transfer_increment_nl) * transfer_increment_nl
            original_volume = round(original_volume / transfer_increment_nl) * transfer_increment_nl
            target = row["Replace Well"].upper()
            if target in targets:
                prior_path, prior_name = targets[target]
                raise ValueError(
                    "Duplicate replacement for {}: '{}' in {} conflicts with '{}' in {}.".format(
                        target, row["Name"], selection.csv_path, prior_name, prior_path
                    )
                )
            targets[target] = (Path(selection.csv_path), row["Name"])
            retained_volumes[target] = original_volume
            if original_volume and not any(base.get("Replacement Well", base["Well"]).upper() == target for base in base_rows):
                raise ValueError("Original staple not found for replacement target {}.".format(target))
            if not replacement_volume:
                continue
            selected_replacements.append(
                {
                    "Well": row["Well"],
                    "Sequence": row["Sequence"],
                    "Sample Comments": (
                        "Tab: {} | Panel: {} | Staple: {} | Sequence: {}".format(
                            selection.replacement_tab,
                            (selection.panel_names or {}).get(row["Name"], ""),
                            row["Name"], row["Sequence"],
                        ) if selection.replacement_tab else row["Sequence"]
                    ),
                    "Source Plate Name": plate_name,
                    "Transfer Volume": replacement_volume,
                }
            )

    active = [
        {"Well": row["Well"], "Sequence": row["Sequence"], "Source Plate Name": base_plate_name,
         "Sample Comments": "Tab: Base | {}Sequence: {}".format(
             "Hinge: {} | ".format("Flexible" if row["Hinge Type"] == "original" else "Rigid")
             if row.get("Hinge Type") else "", row["Sequence"]),
         "Transfer Volume": retained_volumes.get(row.get("Replacement Well", row["Well"]).upper(), transfer_volume_nl)}
        for row in base_rows
        if retained_volumes.get(row.get("Replacement Well", row["Well"]).upper(), transfer_volume_nl) > 0
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
                    "Sample Comments": source.get("Sample Comments", source["Sequence"]),
                    "Transfer Volume": source.get("Transfer Volume", transfer_volume_nl),
                }
            )

    capacity_nl = max_destination_volume_ul * 1000
    destination_index = 0
    used_nl = 0.0
    for transfer in transfers:
        volume_nl = transfer["Transfer Volume"]
        if volume_nl > capacity_nl + 1e-9:
            raise ValueError("Transfer volume is larger than the destination-well capacity.")
        if used_nl + volume_nl > capacity_nl + 1e-9:
            destination_index += 1
            used_nl = 0.0
        if destination_index >= len(destinations):
            raise ValueError("Too many transfers for the destination wells at the requested volumes.")
        transfer["Destination Plate Name"] = destination_plate_name
        transfer["Destination Well"] = destinations[destination_index]
        used_nl += volume_nl
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
    number_of_staples: float,
    scaffold: Sequence[float],
    magnesium: Sequence[float],
    sodium: Sequence[float],
    te_10x: Sequence[float],
    desired: Sequence[float],
) -> List[Dict[str, float]]:
    """Balance DNA, TE, Mg and Na, rejecting infeasible final compositions.

    Staple buffer composition applies to the entire collected staple pool.
    DNA amounts are fixed first; nonnegative buffer additions are solved jointly.
    """
    def vector(values, length, label):
        try:
            result = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise ValueError("{} must contain numeric values.".format(label)) from exc
        if len(result) != length or any(not math.isfinite(value) or value < 0 for value in result):
            raise ValueError("{} must contain {} finite, nonnegative values.".format(label, length))
        return result

    staple = vector(staple, 4, "Staple stock")
    scaffold = vector(scaffold, 4, "Scaffold stock")
    magnesium = vector(magnesium, 4, "Mg stock")
    sodium = vector(sodium, 4, "Na stock")
    te_10x = vector(te_10x, 4, "TE stock")
    desired = vector(desired, 6, "Desired recipe")
    number_of_staples = vector([number_of_staples], 1, "Effective staple count")[0]
    final_volume = desired[0]
    if final_volume <= 0:
        raise ValueError("Final mixing volume must be positive.")
    for label, stock in (("TE", te_10x), ("Mg", magnesium), ("Na", sodium)):
        if stock[0] != 0:
            raise ValueError("{} buffer stock must have 0 nM DNA; supply DNA through staple/scaffold stocks.".format(label))

    def dna_volume(target, stock, label):
        if target == 0:
            return 0.0
        if stock <= 0:
            raise ValueError("{} stock concentration must be positive for a nonzero target.".format(label))
        return final_volume * (target / stock)

    staple_volume = dna_volume(desired[1], staple[0], "Staple") * number_of_staples
    scaffold_volume = dna_volume(desired[2], scaffold[0], "Scaffold")
    volume_tolerance = 1e-9 * max(1.0, final_volume)
    fixed_volume = staple_volume + scaffold_volume
    if not math.isfinite(fixed_volume) or fixed_volume > final_volume + volume_tolerance:
        raise ValueError("Staple mix and scaffold require {:g} µL, exceeding the requested {:g} µL.".format(fixed_volume, final_volume))

    # Solve in units of final concentration, with unknown stock volume fractions.
    stocks = (te_10x, magnesium, sodium)
    matrix = [[stock[component] for stock in stocks] for component in (1, 2, 3)]
    background = [staple_volume/final_volume * staple[c] + scaffold_volume/final_volume * scaffold[c]
                  for c in (1, 2, 3)]
    residual = []
    for label, target, carried in zip(("TE", "Mg", "Na"), desired[3:], background):
        if not math.isfinite(carried) or carried > target + 1e-9 * max(1.0, target):
            raise ValueError("{} carried by staple/scaffold stocks ({:g}) already exceeds the final target ({:g}).".format(label, carried, target))
        residual.append(max(0.0, target - carried))

    # Row scaling keeps TE strengths and salt concentrations numerically comparable.
    scales = [max([abs(value) for value in row] + [abs(target), 1e-300])
              for row, target in zip(matrix, residual)]
    normalized = [[value/scale for value in row] for row, scale in zip(matrix, scales)]
    rhs = [value/scale for value, scale in zip(residual, scales)]

    def solve_square(rows, columns):
        size = len(columns)
        augmented = [[normalized[row][column] for column in columns] + [rhs[row]] for row in rows]
        for column in range(size):
            pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
            if abs(augmented[pivot][column]) <= 1e-12:
                return None
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
            divisor = augmented[column][column]
            augmented[column] = [value/divisor for value in augmented[column]]
            for row in range(size):
                if row != column:
                    factor = augmented[row][column]
                    augmented[row] = [x-factor*y for x,y in zip(augmented[row], augmented[column])]
        return [row[-1] for row in augmented]

    # Enumerate the small system's feasible vertices. This also supports redundant
    # stocks or zero targets; minimum added volume leaves the most room for water.
    candidates = []
    for count in range(4):
        for columns in combinations(range(3), count):
            for rows in combinations(range(3), count):
                solution = solve_square(rows, columns)
                if solution is None or any(not math.isfinite(value) or value < -1e-10 for value in solution):
                    continue
                fractions = [0.0, 0.0, 0.0]
                for column, value in zip(columns, solution):
                    fractions[column] = max(0.0, value)
                achieved = [sum(coefficient*fraction for coefficient,fraction in zip(row, fractions)) for row in matrix]
                if all(math.isfinite(value) and math.isclose(value, target, rel_tol=1e-9, abs_tol=1e-9)
                       for value, target in zip(achieved, residual)):
                    candidates.append(fractions)
    if not candidates:
        raise ValueError("Cannot reach the requested TE, Mg and Na concentrations with nonnegative additions of these stocks. Check stock compositions and targets.")
    additions = min(candidates, key=sum)
    te_volume, magnesium_volume, sodium_volume = [fraction*final_volume for fraction in additions]
    water_volume = final_volume - fixed_volume - sum((te_volume, magnesium_volume, sodium_volume))
    if not math.isfinite(water_volume) or water_volume < -volume_tolerance:
        raise ValueError("Mixing recipe requires {:g} µL of reagents but final volume is {:g} µL (negative water). Use more concentrated stocks or lower targets.".format(final_volume-water_volume, final_volume))
    values = [staple_volume, scaffold_volume, te_volume, magnesium_volume, sodium_volume, max(0.0, water_volume)]
    labels = ["Staple mix (from picklist)", "Scaffold", "{:g}X TE".format(te_10x[1]),
              "Mg ({:g} mM)".format(magnesium[2]), "Na ({:g} mM)".format(sodium[3]), "DI Water"]
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
    """Accept comma/whitespace-separated addresses and normalize A1 to A01."""
    wells = [value.upper() for value in re.split(r"[,;\s]+", text.strip()) if value]
    invalid = [well for well in wells if not re.fullmatch(r"[A-P](?:0?[1-9]|1[0-9]|2[0-4])", well)]
    if invalid:
        raise ValueError(
            "Invalid destination well address(es): {}. Enter individual addresses from A01 to P24 "
            "(for example: A01, A02, A03), not labels or ranges. "
            "Changing a plate label or Used status does not change the Destination wells field.".format(
                ", ".join(invalid)
            )
        )
    return ["{}{:02d}".format(well[0], int(well[1:])) for well in wells]
