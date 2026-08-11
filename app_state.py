"""Persistent user configuration and destination-plate usage state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


PLATE_ROWS = "ABCDEFGHIJKLMNOP"
PLATE_COLUMNS = range(1, 25)


def all_384_wells() -> List[str]:
    return ["{}{:02d}".format(row, column) for row in PLATE_ROWS for column in PLATE_COLUMNS]


def load_json(path: Path, default: Dict) -> Dict:
    path = Path(path)
    if not path.exists():
        return dict(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return value if isinstance(value, dict) else dict(default)


def save_json(path: Path, value: Dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def plate_wells(state: Dict, plate_name: str) -> Dict[str, Dict]:
    plates = state.setdefault("plates", {})
    plate = plates.setdefault(plate_name, {"wells": {}})
    return plate.setdefault("wells", {})


def next_unused_wells(state: Dict, plate_name: str, count: int) -> List[str]:
    used = plate_wells(state, plate_name)
    return [well for well in all_384_wells() if well not in used][: max(1, count)]


def unused_wells_from(state: Dict, plate_name: str, start_well: str, count: int) -> List[str]:
    """Return unused wells at or after a user-selected destination starting well."""
    wells = all_384_wells()
    normalized = str(start_well).strip().upper()
    if normalized not in wells:
        raise ValueError("Destination starting well must be a valid 384-well position (A01–P24).")
    used = plate_wells(state, plate_name)
    if normalized in used:
        raise ValueError(
            "{} is already recorded as used on {}. Choose an unused starting well or start a fresh plate.".format(
                normalized, plate_name
            )
        )
    start = wells.index(normalized)
    result = [well for well in wells[start:] if well not in used][: max(1, count)]
    if len(result) < max(1, count):
        raise ValueError("Not enough unused wells remain at or after {} for {} destination wells.".format(normalized, count))
    return result


def clear_plate(state: Dict, plate_name: str) -> bool:
    """Remove all recorded usage for one named destination plate."""
    plates = state.setdefault("plates", {})
    if plate_name in plates:
        del plates[plate_name]
        state["updated_utc"] = datetime.now(timezone.utc).isoformat()
        return True
    return False


def record_transfers(state: Dict, rows: Sequence[Dict[str, object]]) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    for row in rows:
        plate_name = str(row["Destination Plate Name"])
        well = str(row["Destination Well"]).upper()
        volume_nl = int(row["Transfer Volume"])
        wells = plate_wells(state, plate_name)
        entry = wells.setdefault(well, {"transfer_count": 0, "volume_nL": 0})
        entry["transfer_count"] = int(entry.get("transfer_count", 0)) + 1
        entry["volume_nL"] = int(entry.get("volume_nL", 0)) + volume_nl
        entry["last_used_utc"] = timestamp
    state["updated_utc"] = timestamp


def normalize_recent_wells(values: Iterable[str]) -> List[str]:
    valid = set(all_384_wells())
    result = []
    for value in values:
        well = str(value).strip().upper()
        if well in valid and well not in result:
            result.append(well)
    return result


def create_run_output_directory(root: Path, when: Optional[datetime] = None) -> Path:
    """Create a unique date-and-time subfolder beneath the configured output root."""
    root = Path(root)
    timestamp = (when or datetime.now().astimezone()).strftime("%Y-%m-%d_%H-%M-%S")
    candidate = root / timestamp
    suffix = 2
    while candidate.exists():
        candidate = root / "{}_{:02d}".format(timestamp, suffix)
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate
