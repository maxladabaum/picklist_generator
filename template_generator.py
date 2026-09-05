"""Dependency-free origami barcode template rendering."""

from __future__ import annotations

import json
import math
import struct
import zlib
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


Site = Tuple[int, int]
GROUP_COLORS = (
    "#00c8ff",
    "#ff5a5f",
    "#55d66b",
    "#ffb000",
    "#b77cff",
    "#ff70c8",
    "#00d7b5",
    "#d8d84a",
    "#5f8cff",
    "#ff875f",
    "#8bd450",
    "#cf6fff",
)
# Uniform 12 x 8 lattice pitches whose outer site centers span the measured
# 120 x 35 nm origami footprint. Blank export margin is handled separately.
EXTENSION_COLUMN_SPACING_NM = 120.0 / 11.0
EXTENSION_ROW_SPACING_NM = 35.0 / 7.0


def empty_logical_bit_schema(rows: int, columns: int) -> Dict[str, object]:
    """Return a blank user-editable logical-bit schema for a physical grid."""
    return {
        "format": "paint-analysis-logical-bits-v1",
        "description": "User-defined digital bit groups.",
        "overlap_semantics": "physical-site-union",
        "physical_rows": int(rows),
        "physical_columns": int(columns),
        "alignment_groups": [],
        "logical_bits": [],
    }


def logical_group_color(index: int) -> str:
    """Return the stable display color assigned to a schema group."""
    return GROUP_COLORS[int(index) % len(GROUP_COLORS)]


def _hex_rgb(color: object) -> Tuple[int, int, int]:
    text = str(color).strip()
    if len(text) != 7 or not text.startswith("#"):
        raise ValueError(f"Invalid group display color: {text!r}.")
    try:
        return tuple(int(text[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]
    except ValueError as exc:
        raise ValueError(f"Invalid group display color: {text!r}.") from exc


def logical_stroke_schema(rows: int, columns: int) -> Dict[str, object] | None:
    """Describe the logical strokes for the standard two-panel origami.

    The physical 8 x 12 extension lattice is retained for rendering and rigid
    alignment.  Classification uses four multi-extension logical bits in each
    8 x 6 panel.  Top and left are always-on alignment fiducials.
    """
    if (rows, columns) != (8, 12):
        return None
    panel_width = columns // 2
    alignment_strokes: List[Dict[str, object]] = []
    logical_bits: List[Dict[str, object]] = []
    for panel_index, panel_name in enumerate(("left", "right")):
        offset = panel_index * panel_width
        # Evidence sites deliberately exclude intersections with alignment
        # fiducials or another logical stroke. An intersection is rendered,
        # but is not informative about which touching stroke is active.
        evidence_strokes = {
            "top": [(0, offset + column) for column in range(panel_width)],
            "left": [(row, offset) for row in range(rows)],
            "slash": [
                (row, offset + (rows - 1 - row))
                for row in range(2, rows - 1)
            ],
            "backslash": [
                (row, offset + row)
                for row in range(1, panel_width)
            ],
            "right": [(row, offset + panel_width - 1) for row in range(1, rows - 1)],
            "bottom": [(rows - 1, offset + column) for column in range(1, panel_width - 1)],
        }
        local_strokes = {
            **evidence_strokes,
            "slash": [(0, offset + panel_width - 1), *evidence_strokes["slash"], (rows - 1, offset)],
            # The physical backslash extension series ends before the bottom
            # edge in the established design; its top endpoint is already an
            # alignment-anchor site.
            "backslash": [(0, offset), *evidence_strokes["backslash"]],
            "right": [(row, offset + panel_width - 1) for row in range(rows)],
            "bottom": [(rows - 1, offset + column) for column in range(panel_width)],
        }
        for stroke_name in ("top", "left"):
            alignment_strokes.append(
                {
                    "id": f"{panel_name}.{stroke_name}",
                    "panel": panel_name,
                    "stroke": stroke_name,
                    "physical_sites": [[row + 1, column + 1] for row, column in local_strokes[stroke_name]],
                }
            )
        for stroke_name, label in (
            ("slash", "/"),
            ("backslash", "\\"),
            ("right", "right"),
            ("bottom", "bottom"),
        ):
            logical_bits.append(
                {
                    "id": f"{panel_name}.{stroke_name}",
                    "panel": panel_name,
                    "stroke": stroke_name,
                    "label": f"{panel_name.title()} {label}",
                    "physical_sites": [[row + 1, column + 1] for row, column in local_strokes[stroke_name]],
                    "evidence_sites": [
                        [row + 1, column + 1] for row, column in evidence_strokes[stroke_name]
                    ],
                }
            )
    return {
        "format": "paint-analysis-logical-bits-v1",
        "preset": "two-panel-strokes-v1",
        "description": (
            "Top and left strokes are alignment-only fiducials. Slash, backslash, right, "
            "and bottom are logical bits aggregated across their physical extension sites."
        ),
        "physical_rows": rows,
        "physical_columns": columns,
        "panel_count": 2,
        "panel_columns": panel_width,
        "alignment_strokes": alignment_strokes,
        "logical_bits": logical_bits,
    }


def normalize_logical_bit_schema(
    schema: Dict[str, object],
    rows: int,
    columns: int,
) -> Dict[str, object]:
    """Validate an arbitrary logical-bit schema and return JSON-safe data.

    A bit may contain any nonempty set of physical sites. ``evidence_sites``
    is optional and defaults to all physical sites. Alignment groups may be
    named and shaped freely and are always rendered without becoming bits.
    """
    if not isinstance(schema, dict):
        raise ValueError("Logical-bit schema must be a JSON object.")
    if schema.get("format") not in {
        "paint-analysis-logical-bits-v1",
        "paint-analysis-logical-strokes-v1",
    }:
        raise ValueError("Unsupported logical-bit schema format.")
    if int(schema.get("physical_rows", -1)) != rows or int(schema.get("physical_columns", -1)) != columns:
        raise ValueError("Logical-bit schema dimensions do not match the physical grid.")

    all_used_ids: set[str] = set()
    next_color_index = 0

    def normalized_groups(key: str, *, evidence: bool) -> List[Dict[str, object]]:
        nonlocal next_color_index
        raw_groups = schema.get(key, [])
        if not isinstance(raw_groups, list):
            raise ValueError(f"{key} must be a list.")
        result: List[Dict[str, object]] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                raise ValueError(f"Every {key} entry must be an object.")
            group_id = str(raw_group.get("id", "")).strip()
            if not group_id or group_id in all_used_ids:
                raise ValueError("Every alignment and digital-bit group needs a globally unique nonempty id.")
            all_used_ids.add(group_id)

            def normalized_sites(field: str) -> List[List[int]]:
                raw_sites = raw_group.get(field)
                if not isinstance(raw_sites, list) or not raw_sites:
                    raise ValueError(f"{group_id}.{field} must contain at least one physical site.")
                sites: set[Site] = set()
                for raw_site in raw_sites:
                    if not isinstance(raw_site, (list, tuple)) or len(raw_site) != 2:
                        raise ValueError(f"{group_id}.{field} contains an invalid site.")
                    row, column = int(raw_site[0]), int(raw_site[1])
                    if not (1 <= row <= rows and 1 <= column <= columns):
                        raise ValueError(f"{group_id}.{field} contains a site outside the physical grid.")
                    sites.add((row, column))
                return [[row, column] for row, column in sorted(sites)]

            group = dict(raw_group)
            group["id"] = group_id
            group["label"] = str(raw_group.get("label", group_id))
            display_color = raw_group.get("display_color", logical_group_color(next_color_index))
            _hex_rgb(display_color)
            group["display_color"] = str(display_color).lower()
            next_color_index += 1
            group["physical_sites"] = normalized_sites("physical_sites")
            if evidence:
                if "evidence_sites" in raw_group:
                    group["evidence_sites"] = normalized_sites("evidence_sites")
                else:
                    group["evidence_sites"] = list(group["physical_sites"])
            result.append(group)
        return result

    alignment_key = "alignment_groups" if "alignment_groups" in schema else "alignment_strokes"
    alignment_groups = normalized_groups(alignment_key, evidence=False)
    logical_bits = normalized_groups("logical_bits", evidence=True)
    if not logical_bits:
        raise ValueError("A logical-bit schema must define at least one logical bit.")
    normalized = dict(schema)
    normalized["format"] = "paint-analysis-logical-bits-v1"
    normalized["overlap_semantics"] = "physical-site-union"
    normalized["physical_rows"] = rows
    normalized["physical_columns"] = columns
    normalized["alignment_groups"] = alignment_groups
    normalized.pop("alignment_strokes", None)
    normalized["logical_bits"] = logical_bits
    normalized.pop("active_logical_bits", None)
    return json.loads(json.dumps(normalized))


def logical_template_sites(
    rows: int,
    columns: int,
    active_logical_bits: Iterable[str],
    schema: Dict[str, object] | None = None,
) -> List[Site]:
    """Expand arbitrary logical bits to physical sites needed for rendering."""
    schema = logical_stroke_schema(rows, columns) if schema is None else schema
    if schema is None:
        raise ValueError("No logical-bit schema is defined for this physical lattice.")
    schema = normalize_logical_bit_schema(schema, rows, columns)
    requested = set(active_logical_bits)
    bits = {str(bit["id"]): bit for bit in schema["logical_bits"]}  # type: ignore[index]
    unknown = sorted(requested - set(bits))
    if unknown:
        raise ValueError(f"Unknown logical stroke: {unknown[0]}")
    sites: set[Site] = set()
    for group in schema["alignment_groups"]:  # type: ignore[index]
        sites.update((int(row) - 1, int(column) - 1) for row, column in group["physical_sites"])
    for bit_id in requested:
        sites.update((int(row) - 1, int(column) - 1) for row, column in bits[bit_id]["physical_sites"])
    return sorted(sites)


def infer_active_logical_bits(
    rows: int,
    columns: int,
    selected_sites: Iterable[Site],
    *,
    minimum_coverage: float = 0.6,
    schema: Dict[str, object] | None = None,
) -> List[str]:
    """Infer active bits from coverage of their configured evidence sites."""
    schema = logical_stroke_schema(rows, columns) if schema is None else schema
    if schema is None:
        return []
    schema = normalize_logical_bit_schema(schema, rows, columns)
    selected = set(selected_sites)
    active: List[str] = []
    for bit in schema["logical_bits"]:  # type: ignore[index]
        sites = {
            (int(row) - 1, int(column) - 1)
            for row, column in bit.get("evidence_sites", bit["physical_sites"])
        }
        if sites and len(sites & selected) / len(sites) >= minimum_coverage:
            active.append(str(bit["id"]))
    return active


def validate_template_settings(
    rows: int,
    columns: int,
    spacing_x_nm: float,
    spacing_y_nm: float,
    margin_nm: float,
    spot_sigma_nm: float,
    width_px: int,
) -> None:
    if rows < 1 or columns < 1:
        raise ValueError("Template rows and columns must each be at least 1.")
    if rows > 50 or columns > 50:
        raise ValueError("Template rows and columns must not exceed 50.")
    if spacing_x_nm <= 0 or spacing_y_nm <= 0:
        raise ValueError("Site spacing must be greater than 0 nm.")
    if margin_nm <= 0:
        raise ValueError("Image margin must be greater than 0 nm.")
    if spot_sigma_nm <= 0:
        raise ValueError("Spot sigma must be greater than 0 nm.")
    if not 64 <= width_px <= 4096:
        raise ValueError("Image width must be between 64 and 4096 pixels.")


def template_size_nm(
    rows: int,
    columns: int,
    spacing_x_nm: float,
    spacing_y_nm: float,
    margin_nm: float,
) -> Tuple[float, float]:
    """Return the physical raster footprint containing the requested grid."""
    width_nm = (columns - 1) * spacing_x_nm + 2.0 * margin_nm
    height_nm = (rows - 1) * spacing_y_nm + 2.0 * margin_nm
    return width_nm, height_nm


def render_barcode_template(
    rows: int,
    columns: int,
    selected_sites: Iterable[Site],
    spacing_x_nm: float = EXTENSION_COLUMN_SPACING_NM,
    spacing_y_nm: float = EXTENSION_ROW_SPACING_NM,
    margin_nm: float = 20.0,
    spot_sigma_nm: float = 1.5,
    width_px: int = 500,
    active_logical_bits: Iterable[str] | None = None,
    logical_schema: Dict[str, object] | None = None,
) -> Tuple[int, int, bytes, Dict[str, object]]:
    """Render selected grid sites as equal-brightness Gaussian spots.

    Pixels are returned as row-major 8-bit grayscale values.  The top row in
    ``selected_sites`` is the top row in the saved image.
    """
    validate_template_settings(
        rows, columns, spacing_x_nm, spacing_y_nm, margin_nm, spot_sigma_nm, width_px
    )
    selected = sorted(set(selected_sites))
    invalid = [(row, column) for row, column in selected if not (0 <= row < rows and 0 <= column < columns)]
    if invalid:
        raise ValueError("Selected site is outside the template grid: {}".format(invalid[0]))
    if not selected:
        raise ValueError("Select at least one barcode site before saving a template.")

    width_nm, height_nm = template_size_nm(rows, columns, spacing_x_nm, spacing_y_nm, margin_nm)
    height_px = max(1, int(round(width_px * height_nm / width_nm)))
    if height_px > 4096:
        raise ValueError("Computed image height exceeds 4096 pixels; reduce the image width.")
    logical_model = logical_schema
    active_ids: List[str] = []
    site_colors: Dict[Site, List[Tuple[int, int, int]]] = {}
    if logical_model is not None:
        logical_model = normalize_logical_bit_schema(logical_model, rows, columns)
        active_ids = (
            sorted(set(str(value) for value in active_logical_bits))
            if active_logical_bits is not None
            else infer_active_logical_bits(rows, columns, selected, schema=logical_model)
        )
        known_ids = {str(bit["id"]) for bit in logical_model["logical_bits"]}  # type: ignore[index]
        unknown_ids = sorted(set(active_ids) - known_ids)
        if unknown_ids:
            raise ValueError(f"Unknown active logical bit: {unknown_ids[0]}")
        rendered_groups = list(logical_model["alignment_groups"]) + [  # type: ignore[index]
            bit for bit in logical_model["logical_bits"] if str(bit["id"]) in active_ids  # type: ignore[index]
        ]
        for group in rendered_groups:
            color = _hex_rgb(group["display_color"])
            for one_based_row, one_based_column in group["physical_sites"]:
                site_colors.setdefault((int(one_based_row) - 1, int(one_based_column) - 1), []).append(color)

    channels = 3 if logical_model is not None else 1
    pixels = bytearray(width_px * height_px * channels)
    sigma_x_px = spot_sigma_nm * (width_px - 1) / width_nm
    sigma_y_px = spot_sigma_nm * (height_px - 1) / height_nm
    radius_x = max(1, int(math.ceil(4.0 * sigma_x_px)))
    radius_y = max(1, int(math.ceil(4.0 * sigma_y_px)))

    sites = []
    for row, column in selected:
        x_nm = margin_nm + column * spacing_x_nm
        y_nm = margin_nm + row * spacing_y_nm
        center_x = x_nm * (width_px - 1) / width_nm
        center_y = y_nm * (height_px - 1) / height_nm
        x0 = max(0, int(math.floor(center_x)) - radius_x)
        x1 = min(width_px - 1, int(math.ceil(center_x)) + radius_x)
        y0 = max(0, int(math.floor(center_y)) - radius_y)
        y1 = min(height_px - 1, int(math.ceil(center_y)) + radius_y)
        for py in range(y0, y1 + 1):
            dy = (py - center_y) / sigma_y_px
            for px in range(x0, x1 + 1):
                dx = (px - center_x) / sigma_x_px
                value = int(round(255.0 * math.exp(-0.5 * (dx * dx + dy * dy))))
                offset = (py * width_px + px) * channels
                colors = site_colors.get((row, column), [(255, 255, 255)])
                if channels == 1:
                    if value > pixels[offset]:
                        pixels[offset] = value
                else:
                    # A site shared by groups receives their additive/blended
                    # color. Every palette color has a full-strength channel,
                    # so Paint Analysis can recover equal alignment intensity
                    # using channel maximum rather than luminance.
                    blended = tuple(max(color[channel] for color in colors) for channel in range(3))
                    for channel, component in enumerate(blended):
                        colored_value = int(round(value * component / 255.0))
                        if colored_value > pixels[offset + channel]:
                            pixels[offset + channel] = colored_value
        sites.append(
            {
                "site": row * columns + column + 1,
                "row": row + 1,
                "column": column + 1,
                "x_nm": x_nm,
                "y_nm_from_top": y_nm,
            }
        )

    metadata: Dict[str, object] = {
        "format": "paint-analysis-origami-template-v1",
        "description": "Bright expected localization sites on a dark background.",
        "rows": rows,
        "columns": columns,
        "spacing_x_nm": spacing_x_nm,
        "spacing_y_nm": spacing_y_nm,
        "margin_nm": margin_nm,
        "spot_sigma_nm": spot_sigma_nm,
        "width_nm": width_nm,
        "height_nm": height_nm,
        "width_px": width_px,
        "height_px": height_px,
        # Pixel-center spacing across the complete raster. The configured
        # margin remains blank physical padding around the docking-site grid.
        "pixel_size_x_nm": width_nm / max(width_px - 1, 1),
        "pixel_size_y_nm": height_nm / max(height_px - 1, 1),
        "selected_site_count": len(sites),
        "selected_sites": sites,
    }
    if logical_model is not None:
        logical_model["active_logical_bits"] = active_ids
        metadata["logical_model"] = logical_model
        metadata["png_channels"] = "RGB group colors"
    return width_px, height_px, bytes(pixels), metadata


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def write_grayscale_png(
    path: Path,
    width: int,
    height: int,
    pixels: Sequence[int],
    metadata: Dict[str, object] | None = None,
) -> None:
    """Write an 8-bit grayscale or RGB PNG using only the standard library."""
    if len(pixels) == width * height:
        channels, color_type = 1, 0
    elif len(pixels) == width * height * 3:
        channels, color_type = 3, 2
    else:
        raise ValueError("Pixel buffer size does not match the requested PNG dimensions.")
    raw = b"".join(
        b"\x00" + bytes(pixels[row * width * channels : (row + 1) * width * channels])
        for row in range(height)
    )
    chunks = [
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
    ]
    if metadata is not None:
        encoded_metadata = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        chunks.append(_png_chunk(b"tEXt", b"paint_analysis_template\x00" + encoded_metadata))
    chunks.extend((_png_chunk(b"IDAT", zlib.compress(raw, 9)), _png_chunk(b"IEND", b"")))
    data = b"\x89PNG\r\n\x1a\n" + b"".join(chunks)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def save_barcode_template(
    path: Path,
    rows: int,
    columns: int,
    selected_sites: Iterable[Site],
    spacing_x_nm: float = EXTENSION_COLUMN_SPACING_NM,
    spacing_y_nm: float = EXTENSION_ROW_SPACING_NM,
    margin_nm: float = 20.0,
    spot_sigma_nm: float = 1.5,
    width_px: int = 500,
    active_logical_bits: Iterable[str] | None = None,
    logical_schema: Dict[str, object] | None = None,
) -> Tuple[Path, Path, Dict[str, object]]:
    """Save a PNG template and a same-name JSON metadata sidecar."""
    path = Path(path)
    if path.suffix.lower() != ".png":
        path = path.with_suffix(".png")
    width, height, pixels, metadata = render_barcode_template(
        rows,
        columns,
        selected_sites,
        spacing_x_nm,
        spacing_y_nm,
        margin_nm,
        spot_sigma_nm,
        width_px,
        active_logical_bits,
        logical_schema,
    )
    write_grayscale_png(path, width, height, pixels, metadata)
    metadata_path = path.with_suffix(".json")
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return path, metadata_path, metadata
