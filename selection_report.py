"""Dependency-free PDF export for selected replacement panels."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
from textwrap import wrap
from typing import Dict, List, Optional, Sequence, Tuple


PAGE_WIDTH = 792
PAGE_HEIGHT = 612
MARGIN = 24
CARD_GAP = 12
CARD_WIDTH = 366


def _grid_metrics(panel: Dict[str, object]) -> Tuple[float, float, float, float]:
    """Return x/y center pitch and marker width/height for a panel grid."""
    columns = list(panel.get("columns", []))  # type: ignore[arg-type]
    column_pitch = min(24.0, 288.0 / max(1, len(columns)))
    spacing_x = panel.get("spacing_x_nm")
    spacing_y = panel.get("spacing_y_nm")
    if spacing_x is not None and spacing_y is not None:
        x_nm, y_nm = float(spacing_x), float(spacing_y)
        if x_nm > 0 and y_nm > 0:
            row_pitch = column_pitch * y_nm / x_nm
            return column_pitch, row_pitch, min(10.0, column_pitch * 0.55), min(8.0, row_pitch * 0.7)
    return column_pitch, 20.0, max(4.0, column_pitch - 3.0), 16.0


def _pdf_text(value: object) -> str:
    text = str(value).translate(
        str.maketrans({"—": "-", "–": "-", "•": "|", "µ": "u", "×": "x"})
    )
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text(x: float, top: float, value: object, size: int = 10, bold: bool = False, color=(0.2, 0.25, 0.3)) -> str:
    y = PAGE_HEIGHT - top
    font = "F2" if bold else "F1"
    return "BT /{} {} Tf {:.3f} {:.3f} {:.3f} rg {:.2f} {:.2f} Td ({}) Tj ET".format(
        font, size, color[0], color[1], color[2], x, y, _pdf_text(value)
    )


def _rect(x: float, top: float, width: float, height: float, fill, stroke=(0.72, 0.76, 0.8)) -> str:
    y = PAGE_HEIGHT - top - height
    return (
        "{:.3f} {:.3f} {:.3f} rg {:.3f} {:.3f} {:.3f} RG "
        "{:.2f} {:.2f} {:.2f} {:.2f} re B"
    ).format(
        fill[0], fill[1], fill[2], stroke[0], stroke[1], stroke[2],
        x, y, width, height,
    )


def _card_height(panel: Dict[str, object]) -> int:
    rows = list(panel.get("rows", []))  # type: ignore[arg-type]
    names = [str(value) for value in panel.get("selected_names", [])]  # type: ignore[union-attr]
    name_lines = wrap(", ".join(names), width=56, break_long_words=False) or [""]
    _column_pitch, row_pitch, _marker_width, marker_height = _grid_metrics(panel)
    return int(math.ceil(76 + len(rows) * row_pitch + marker_height + len(name_lines) * 12))


def _layout_pages(
    panels: Sequence[Dict[str, object]], start_y: int = 76
) -> List[List[Tuple[Dict[str, object], int, int, int]]]:
    pages: List[List[Tuple[Dict[str, object], int, int, int]]] = []
    page: List[Tuple[Dict[str, object], int, int, int]] = []
    column_y = [start_y, start_y]
    bottom_limit = PAGE_HEIGHT - MARGIN
    for panel in panels:
        height = _card_height(panel)
        available = [index for index, y in enumerate(column_y) if y + height <= bottom_limit]
        if not available:
            pages.append(page)
            page = []
            column_y = [start_y, start_y]
            available = [0, 1]
        column = min(available, key=lambda index: column_y[index])
        x = MARGIN + column * (CARD_WIDTH + CARD_GAP)
        y = column_y[column]
        page.append((panel, x, y, height))
        column_y[column] += height + CARD_GAP
    if page:
        pages.append(page)
    return pages


def _page_commands(
    cards: Sequence[Tuple[Dict[str, object], int, int, int]],
    page_number: int,
    page_count: int,
    selected_total: int,
    generated: str,
    title: str = "Replacement selections",
    context: str = "",
) -> bytes:
    details = [context] if context else []
    details.extend(("{} selected".format(selected_total), generated, "page {} of {}".format(page_number, page_count)))
    commands = [
        _rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT, (0.96, 0.97, 0.98), (0.96, 0.97, 0.98)),
        _text(MARGIN, 32, title, 19, True, (0.09, 0.13, 0.17)),
        _text(MARGIN, 52, " | ".join(details), 9, False, (0.32, 0.38, 0.43)),
    ]

    for panel, card_x, card_y, card_height in cards:
        group = str(panel.get("group", ""))
        label = str(panel.get("label", ""))
        rows = [str(value) for value in panel.get("rows", [])]  # type: ignore[union-attr]
        columns = [str(value) for value in panel.get("columns", [])]  # type: ignore[union-attr]
        active = {tuple(value) for value in panel.get("active", [])}  # type: ignore[union-attr]
        selected = {tuple(value) for value in panel.get("selected", [])}  # type: ignore[union-attr]
        names = [str(value) for value in panel.get("selected_names", [])]  # type: ignore[union-attr]
        name_lines = wrap(", ".join(names), width=56, break_long_words=False) or [""]

        commands.append(_rect(card_x, card_y, CARD_WIDTH, card_height, (1, 1, 1)))
        commands.append(_text(card_x + 12, card_y + 20, "{} - {}".format(group, label), 12, True, (0.09, 0.13, 0.17)))
        commands.append(_text(card_x + 12, card_y + 36, "{} selected".format(len(selected)), 8, False, (0.32, 0.38, 0.43)))

        column_pitch, row_pitch, marker_width, marker_height = _grid_metrics(panel)
        grid_x = card_x + 54
        grid_y = card_y + 45
        for column_index, column in enumerate(columns):
            center_x = grid_x + (column_index + 0.5) * column_pitch
            commands.append(_text(center_x - 4, grid_y + 8, column, 6, False, (0.32, 0.38, 0.43)))
        for row_index, row in enumerate(rows):
            center_y = grid_y + 16 + row_index * row_pitch
            top = center_y - marker_height / 2
            commands.append(_text(grid_x - 34, center_y + 3, row, 7, False, (0.32, 0.38, 0.43)))
            for column_index, column in enumerate(columns):
                position = (row, column)
                center_x = grid_x + (column_index + 0.5) * column_pitch
                x = center_x - marker_width / 2
                if position in selected:
                    fill, stroke = (0.21, 0.66, 0.36), (0.09, 0.45, 0.23)
                elif position in active:
                    fill, stroke = (1, 1, 1), (0.60, 0.65, 0.69)
                else:
                    fill, stroke = (0.85, 0.87, 0.89), (0.72, 0.75, 0.78)
                commands.append(_rect(x, top, marker_width, marker_height, fill, stroke))

        names_top = grid_y + 24 + len(rows) * row_pitch
        commands.append(_text(card_x + 12, names_top, "Selected replacements:", 8, True))
        for line_index, line in enumerate(name_lines, 1):
            commands.append(_text(card_x + 12, names_top + line_index * 11, line, 7))

    return ("\n".join(commands) + "\n").encode("latin-1")


def _build_pdf(streams: Sequence[bytes]) -> bytes:
    objects: List[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"]
    page_references: List[int] = []
    for stream in streams:
        content_number = len(objects) + 1
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        page_number = len(objects) + 1
        page_references.append(page_number)
        objects.append(
            ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {} {}] "
             "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {} 0 R >>").format(
                PAGE_WIDTH, PAGE_HEIGHT, content_number
            ).encode("ascii")
        )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = "<< /Type /Pages /Count {} /Kids [{}] >>".format(
        len(page_references), " ".join("{} 0 R".format(number) for number in page_references)
    ).encode("ascii")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend("{} 0 obj\n".format(number).encode("ascii"))
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend("xref\n0 {}\n".format(len(objects) + 1).encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend("{:010d} 00000 n \n".format(offset).encode("ascii"))
    output.extend(
        ("trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n").format(
            len(objects) + 1, xref_offset
        ).encode("ascii")
    )
    return bytes(output)


def write_replacement_selection_pdf(
    path: Path,
    panels: Sequence[Dict[str, object]],
    when: Optional[datetime] = None,
) -> None:
    """Write a multi-page PDF report containing every panel with selections."""
    if not panels:
        raise ValueError("Select at least one replacement before saving a PDF.")
    pages = _layout_pages(panels)
    selected_total = sum(len(panel.get("selected_names", [])) for panel in panels)  # type: ignore[arg-type]
    generated = (when or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S %Z")
    streams = [
        _page_commands(cards, index, len(pages), selected_total, generated)
        for index, cards in enumerate(pages, 1)
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_build_pdf(streams))


def write_stored_runs_selection_pdf(
    path: Path,
    runs: Sequence[Dict[str, object]],
    when: Optional[datetime] = None,
) -> None:
    """Write selected replacement panels for each stored run, starting every run on a new page."""
    if not runs:
        raise ValueError("Select at least one stored run before saving a PDF.")
    generated = (when or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S %Z")
    page_specs: List[Tuple[str, int, List[Tuple[Dict[str, object], int, int, int]]]] = []
    for run_index, run in enumerate(runs, 1):
        run_name = str(run.get("run_name", "")).strip() or "Run {}".format(run_index)
        panels = run.get("panels", [])
        if not isinstance(panels, list) or not panels:
            raise ValueError("'{}' does not contain replacement-selection metadata.".format(run_name))
        selected_total = sum(len(panel.get("selected_names", [])) for panel in panels)
        for cards in _layout_pages(panels):
            page_specs.append((run_name, selected_total, cards))

    streams = [
        _page_commands(
            cards,
            page_number,
            len(page_specs),
            selected_total,
            generated,
            title="Stored picklist selections",
            context="Run: {}".format(run_name),
        )
        for page_number, (run_name, selected_total, cards) in enumerate(page_specs, 1)
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_build_pdf(streams))
