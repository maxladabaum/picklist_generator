"""Independent miRNA panels with page labels and R1-aligned physical positions."""

import re
from pathlib import Path

from picklist_core import parse_source2


TARGETS = ("miR-21", "miR-141", "miR-155", "miR-210", "miR-223")


def anchor_position(name):
    """Read the page and site from the sequence table's Name, never its plate diagram."""
    match = re.search(r"_(p[12])_H(\d+)-(\d+)(?:_|$)", name)
    if not match:
        raise ValueError(f"Missing anchor position in Name: {name}")
    page, helix, column = match.groups()
    return page, f"H{int(helix):02d}", f"{int(column):02d}"


def anchor_label(name):
    page, helix, column = anchor_position(name)
    return f"{page}.H{int(helix[1:])}-{int(column)}"


def replacement_positions(sheet_dir: Path):
    """Translate page-local anchor names into the existing R1 coordinate system.

    The page designation remains part of the source name. Match normalized base
    replacement wells instead of guessing a column mirror or helix offset.
    """
    anchors, _ = parse_source2(sheet_dir / "miRNA_replace.csv")
    r1, _ = parse_source2(sheet_dir / "PAINT_R1_replace.csv")
    by_well = {}
    for row in r1:
        match = re.fullmatch(r"U-Apt_H(\d+)-(\d+)_R1", row["Name"])
        if not match:
            continue
        helix, column = map(int, match.groups())
        if helix not in range(1, 16, 2) or column not in range(1, 13):
            raise ValueError("R1 site outside the 8×12 grid: " + row["Name"])
        site = (f"H{helix:02d}", f"{column:02d}")
        key = row["Replace Well"].upper()
        if key in by_well and by_well[key] != site:
            raise ValueError("Ambiguous R1 coordinates for replacement well " + key)
        by_well[key] = site
    positions = {}
    for row in anchors:
        anchor_position(row["Name"])  # Validate and preserve the page-local source designation.
        key = row["Replace Well"].upper()
        if key not in by_well:
            raise ValueError("No comparable R1 site for {} (replacement well {}).".format(row["Name"], key))
        positions[row["Name"]] = by_well[key]
    return positions


def target_designs(sheet_dir: Path):
    locks, _ = parse_source2(sheet_dir / "miRNA_replace.csv")
    by_well = {row["Well"].upper(): row for row in locks}
    common = [by_well[well] for well in ("C8", "C10", "C12")]
    combined = [by_well[well] for well in ("C14", "C16", "C18")]
    for plain, dual in zip(common, combined):
        if plain["Replace Well"] != dual["Replace Well"]:
            raise ValueError("Common and R1–lock staples must replace the same well.")
    designs = {}
    for target, source_row in zip(TARGETS, "CDEFG"):
        target_locks = [by_well[f"{source_row}{column}"] for column in (2, 4, 6)]
        if any(not row["Name"].startswith(target.replace("-", "") + "Lock") for row in target_locks):
            raise ValueError(f"Unexpected target lock assignment for {target}.")
        designs[target] = {
            "target_locks": [row["Name"] for row in target_locks],
            "common_locks": [row["Name"] for row in common],
            "combined_locks": [row["Name"] for row in combined],
            "target_wells": [row["Well"] for row in target_locks],
        }
    return designs

