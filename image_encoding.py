"""RGB image sampling, barcode encoding, and independent Echo runs."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from picklist_core import ReplacementSelection, generate_picklist, replacement_fractions, calculate_mixing_volumes, parse_source1, parse_source2, valid_transfer, validate_transfer_limits
from template_generator import EXTENSION_COLUMN_SPACING_NM, EXTENSION_ROW_SPACING_NM, EXTENSION_EXTRA_COLUMN_GAP_NM

IMAGE_WIDTH, IMAGE_HEIGHT = 24, 32
PIXEL_X_NM = EXTENSION_COLUMN_SPACING_NM / 4
PIXEL_Y_NM = EXTENSION_ROW_SPACING_NM / 4
ROWS = ["H{:02d}".format(row) for row in range(1, 16, 2)]
IMAGE_COLUMNS = ["{:02d}".format(column) for column in range(6, 0, -1)]
COLOR_BITS = {"R1 R": (True, True), "R1 B": (True, False), "R1 G": (False, False)}
CHANNELS = {"R1 R": 0, "R1 G": 1, "R1 B": 2}
SHIFT_STATES = {"0 — off / off": (False, False), "1 — off / on": (False, True),
                "2 — on / off": (True, False), "3 — on / on": (True, True)}
BIT_SITES = [tuple((row, column) for row in rows) for rows, column in (
    (("H01", "H03", "H05"), "11"), (("H01", "H03", "H05"), "09"),
    (("H01", "H03", "H05"), "07"), (("H11", "H13", "H15"), "11"),
    (("H11", "H13", "H15"), "09"), (("H11", "H13", "H15"), "07"))]
NINE_BIT_SITES = [tuple((row, column) for row in rows)
                  for rows, columns in ((("H01", "H03"), ("12", "10", "08")),
                                        (("H07", "H09"), ("11", "09", "07")),
                                        (("H13", "H15"), ("12", "10", "08")))
                  for column in columns]
SEVEN_BIT_SITES = NINE_BIT_SITES[:7]
BARCODE_SCHEMA_KEYS = {6: "six_bit", 7: "seven_bit", 9: "nine_bit"}


FIT_MODES = ("Fit with black padding", "Center crop", "Stretch")


def default_barcode_schema():
    return {"version": 1, "name": "Standard barcode sites", "always_on_sites": [],
            **{key: [{"bit": i+1, "sites": ["{}-{}".format(*site) for site in sites]}
                     for i, sites in enumerate(groups)]
               for key, groups in (("six_bit", BIT_SITES), ("seven_bit", SEVEN_BIT_SITES), ("nine_bit", NINE_BIT_SITES))}}


def validate_barcode_schema(schema, bit_count=None):
    """Validate/copy the site map; check fiducial conflicts only for bit_count.

    Without a selected family, validate structure and retain both mappings.
    Geometry and compilation must pass their actual six- or nine-bit count.
    """
    if not isinstance(schema, dict) or type(schema.get("version")) is not int or schema["version"] != 1:
        raise ValueError("Barcode schema must be an object with version 1.")
    if set(schema) - {"version", "name", "six_bit", "seven_bit", "nine_bit", "always_on_sites"}:
        raise ValueError("Unknown barcode schema field. Use version, name, six_bit, seven_bit, nine_bit, or always_on_sites.")
    if not any(key in schema for key in ("six_bit", "seven_bit", "nine_bit")):
        raise ValueError("Provide six_bit, seven_bit, or nine_bit site definitions.")
    name = schema.get("name", "Custom barcode sites")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Barcode schema name must be nonempty text.")
    result = default_barcode_schema()
    result["name"] = name.strip()
    valid_sites = {"{}-{:02d}".format(row, column) for row in ROWS for column in range(7, 13)}
    for key, count in (("six_bit", 6), ("seven_bit", 7), ("nine_bit", 9)):
        if key not in schema:
            continue
        groups = schema[key]
        if not isinstance(groups, list) or len(groups) != count:
            raise ValueError("{} must define exactly {} bits.".format(key, count))
        by_bit, used = {}, set()
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"bit", "sites"}:
                raise ValueError("Each bit needs only bit and sites fields.")
            bit, sites = group["bit"], group["sites"]
            if type(bit) is not int or bit not in range(1, count+1) or bit in by_bit:
                raise ValueError("{} must contain each bit from 1 through {} once.".format(key, count))
            if not isinstance(sites, list) or not sites:
                raise ValueError("Each barcode bit must have at least one site.")
            for site in sites:
                if not isinstance(site, str) or site not in valid_sites:
                    raise ValueError("Barcode site {!r} is outside the supported RGB barcode region. "
                                     "Digital bits must use template columns C7–C12 and rows R1–R8 "
                                     "(H01/H03/…/H15, for example H01-11). Template columns C1–C6 "
                                     "are currently reserved for image pixels.".format(site))
                if site in used:
                    raise ValueError("Barcode site {} is assigned more than once in {}.".format(site, key))
                used.add(site)
            by_bit[bit] = {"bit": bit, "sites": list(sites)}
        result[key] = [by_bit[i] for i in range(1, count+1)]
    always_on = schema.get("always_on_sites", [])
    valid_fiducials = {"{}-{:02d}".format(row, column) for row in ROWS for column in range(1, 13)}
    if not isinstance(always_on, list) or any(not isinstance(site, str) or site not in valid_fiducials for site in always_on):
        raise ValueError("always_on_sites must list valid H01/H03/…/H15 sites on rows 01–12.")
    if len(always_on) != len(set(always_on)):
        raise ValueError("Each always-on site must appear only once.")
    if bit_count not in (None, 6, 7, 9):
        raise ValueError("Barcode bit count must be 6, 7, or 9.")
    for key in (() if bit_count is None else (BARCODE_SCHEMA_KEYS[bit_count],)):
        occupied = {site for bit in result[key] for site in bit["sites"]}
        overlap = occupied.intersection(always_on)
        if overlap:
            raise ValueError("Always-on sites overlap {} barcode bits: {}. Remove those sites from the bit mapping first.".format(key, ", ".join(sorted(overlap))))
    result["always_on_sites"] = list(always_on)
    return result


def fiducial_sites(schema=None, active_image_columns=None, bit_count=None, *, validate_overlaps=True):
    value = default_barcode_schema() if schema is None else validate_barcode_schema(schema, bit_count if validate_overlaps else None)
    sites = [tuple(site.split("-")) for site in value["always_on_sites"]]
    overlap = ["-".join(site) for site in sites if site[1] in active_columns(active_image_columns)]
    if overlap and validate_overlaps:
        raise ValueError("Always-on sites overlap the active analog canvas: {}. Exclude their analog rows first.".format(", ".join(overlap)))
    return sites


def barcode_site_groups(bit_count, schema=None, *, validate_overlaps=True):
    value = default_barcode_schema() if schema is None else validate_barcode_schema(schema, bit_count if validate_overlaps else None)
    return [[tuple(site.split("-")) for site in bit["sites"]]
            for bit in value[BARCODE_SCHEMA_KEYS[bit_count]]]


def active_columns(columns=None):
    """Canonical physical order of selected analog rows (06 through 01)."""
    if columns is None:
        return list(IMAGE_COLUMNS)
    values = list(columns)
    if not values or len(values) != len(set(values)) or any(c not in IMAGE_COLUMNS for c in values):
        raise ValueError("Select at least one analog row from 06 through 01, without duplicates.")
    return [c for c in IMAGE_COLUMNS if c in values]


def canvas_span(columns=None):
    values = active_columns(columns)
    return int(values[0]) - int(values[-1]) + 1


@dataclass(frozen=True)
class EncodingScheme:
    label: str
    x_states: int
    y_states: int
    joint: bool = True
    bit_count: int = 6
    parity: bool = False
    tiled: bool = False
    parent_scheme: str = ""
    tiles_x: int = 0
    tiles_y: int = 0
    position_codebook: bool = False

    @property
    def tile_columns(self):
        return self.tiles_x or (2 if self.tiled else 1)

    @property
    def tile_rows(self):
        return self.tiles_y or (2 if self.tiled else 1)

    @property
    def tile_count(self):
        return self.tile_columns * self.tile_rows

    @staticmethod
    def _counts(states, tiles):
        return tuple(states // tiles + (i < states % tiles) for i in range(tiles))

    @property
    def x_counts(self):
        return self._counts(self.x_states, self.tile_columns)

    @property
    def y_counts(self):
        return self._counts(self.y_states, self.tile_rows)

    @property
    def bit_sites(self):
        return {6: BIT_SITES, 7: SEVEN_BIT_SITES, 9: NINE_BIT_SITES}[self.bit_count]

    @property
    def design_count(self):
        return 3 * self.x_states * self.y_states


DEFAULT_SCHEME = "fixed_4x4"
ENCODING_SCHEMES = {
    DEFAULT_SCHEME: EncodingScheme("4×4 — 24×32 pixels, 48 designs (original)", 4, 4, False),
    "joint_6x3": EncodingScheme("6×3 — 36×24 pixels, 54 designs (nearly square pixels)", 6, 3),
    "joint_7x3": EncodingScheme("7×3 — 42×24 pixels, 63 designs (maximum six-bit capacity)", 7, 3),
    "joint_5x4": EncodingScheme("5×4 — 30×32 pixels, 60 designs", 5, 4),
    "quadrants_2x2": EncodingScheme("4 quadrants — 24×32 pixels, 48 designs, 118.8×81.6 nm", 4, 4, False, tiled=True, parent_scheme=DEFAULT_SCHEME),
    "quadrants_joint_6x3": EncodingScheme("4 quadrants · 6×3 budget — 36×24 samples, 54 types", 6, 3, tiled=True, parent_scheme="joint_6x3"),
    "quadrants_joint_7x3": EncodingScheme("4 quadrants · 7×3 budget — 42×24 samples, 63 types", 7, 3, tiled=True, parent_scheme="joint_7x3"),
    "quadrants_joint_5x4": EncodingScheme("4 quadrants · 5×4 budget — 30×32 samples, 60 types", 5, 4, tiled=True, parent_scheme="joint_5x4"),
    "nine_fixed_16x8": EncodingScheme("9-bit fixed 16×8 — 96×64 pixels, 384 designs", 16, 8, False, 9),
    "nine_joint_18x9": EncodingScheme("9-bit joint 18×9 — 108×72 pixels, 486 designs", 18, 9, True, 9),
    "nine_parity_8x8": EncodingScheme("9-bit parity 8×8 — 48×64 pixels, 192 designs", 8, 8, False, 9, True),
}


# Every allocation of four position bits, available for comparison and production.
TILING_SCHEMES = {}
for _tx in range(5):
    for _ty in range(5 - _tx):
        for _sx in range(5 - _tx - _ty):
            _sy = 4 - _tx - _ty - _sx
            _key = "tiling_{}_{}_{}_{}".format(_tx, _ty, _sx, _sy)
            TILING_SCHEMES[_key] = EncodingScheme(
                "{}×{} tiles · {}×{} local shifts".format(2**_tx, 2**_ty, 2**_sx, 2**_sy),
                2**(_tx + _sx), 2**(_ty + _sy), False,
                tiled=bool(_tx + _ty), tiles_x=2**_tx, tiles_y=2**_ty)


# Enumerate every ordered factorization of each joint position budget, then
# every nonempty tile count. Local shifts are balanced across equal-sized tiles.
def _shift_label(counts):
    return str(counts[0]) if min(counts) == max(counts) else '{}–{}'.format(min(counts), max(counts))


JOINT_TILING_SCHEMES = {}
for _parent in (DEFAULT_SCHEME, 'joint_6x3', 'joint_7x3', 'joint_5x4'):
    _base = ENCODING_SCHEMES[_parent]
    _budget = _base.x_states * _base.y_states
    for _xs in range(1, _budget + 1):
        if _budget % _xs:
            continue
        _ys = _budget // _xs
        for _nx in range(1, _xs + 1):
            for _ny in range(1, _ys + 1):
                _xc = EncodingScheme._counts(_xs, _nx)
                _yc = EncodingScheme._counts(_ys, _ny)
                # Preserve identifiers already stored in user runs/configurations.
                _stem = _parent if (_xs, _ys) == (_base.x_states, _base.y_states) else '{}_states_{}x{}'.format(_parent, _xs, _ys)
                _key = 'sweep_{}_tiles_{}x{}'.format(_stem, _nx, _ny)
                JOINT_TILING_SCHEMES[_key] = EncodingScheme(
                    '{}×{} budget · {}×{} tiles · {}×{} local shifts'.format(
                        _xs, _ys, _nx, _ny, _shift_label(_xc), _shift_label(_yc)),
                    _xs, _ys, joint=(_parent != DEFAULT_SCHEME), tiled=(_nx * _ny > 1),
                    position_codebook=(_parent == DEFAULT_SCHEME),
                    parent_scheme=_parent, tiles_x=_nx, tiles_y=_ny)

SWEEP_SCHEMES = {**TILING_SCHEMES, **JOINT_TILING_SCHEMES}
# Direct tile-count layouts use one origami per color per tile. Larger layouts
# use the nine-bit family to preserve unique positions and the original color bits.
MAX_TILES_PER_COLOR = 128
MAX_SEVEN_BIT_TILES = (1 << 7) // len(CHANNELS)
TILE_COUNT_SCHEMES = {}
for _count in range(1, MAX_TILES_PER_COLOR + 1):
    for _columns in range(1, _count + 1):
        if _count % _columns:
            continue
        _rows = _count // _columns
        _key = "tiles_{}x{}".format(_columns, _rows)
        TILE_COUNT_SCHEMES[_key] = EncodingScheme(
            "{} tiles · {}×{} grid · {} RGB runs".format(_count, _columns, _rows, 3 * _count),
            _columns, _rows, joint=False, bit_count=6 if _count <= 16 else 9, tiled=(_count > 1),
            tiles_x=_columns, tiles_y=_rows, position_codebook=True)

# Seven-bit joint codes cover all three colors without reserving color bits.
SEVEN_BIT_TILE_SCHEMES = {
    "seven_" + key: EncodingScheme(
        "7-bit joint · {} tiles · {}×{} grid · {} RGB runs".format(
            scheme.tile_count, scheme.tile_columns, scheme.tile_rows, scheme.design_count),
        scheme.x_states, scheme.y_states, joint=True, bit_count=7,
        tiled=scheme.tiled, tiles_x=scheme.tile_columns, tiles_y=scheme.tile_rows)
    for key, scheme in TILE_COUNT_SCHEMES.items()
    if scheme.tile_count <= MAX_SEVEN_BIT_TILES
}

SIX_BIT_JOINT_TILE_SCHEMES = {
    "six_joint_" + key: EncodingScheme(
        "6-bit joint · " + scheme.label, scheme.x_states, scheme.y_states,
        joint=True, bit_count=6, tiled=True,
        tiles_x=scheme.tile_columns, tiles_y=scheme.tile_rows)
    for key, scheme in TILE_COUNT_SCHEMES.items() if 16 < scheme.tile_count <= 21
}

NINE_BIT_SMALL_TILE_SCHEMES = {
    "nine_" + key: EncodingScheme(
        "9-bit fixed · " + scheme.label, scheme.x_states, scheme.y_states,
        joint=False, bit_count=9, tiled=scheme.tiled,
        tiles_x=scheme.tile_columns, tiles_y=scheme.tile_rows, position_codebook=True)
    for key, scheme in TILE_COUNT_SCHEMES.items() if scheme.bit_count == 6
}

SELECTABLE_SCHEMES = {**ENCODING_SCHEMES, **SWEEP_SCHEMES, **TILE_COUNT_SCHEMES, **SEVEN_BIT_TILE_SCHEMES, **NINE_BIT_SMALL_TILE_SCHEMES, **SIX_BIT_JOINT_TILE_SCHEMES}

def schemes_for_bits(bit_count):
    return {key: scheme for key, scheme in SELECTABLE_SCHEMES.items() if scheme.bit_count == bit_count}


def direct_tile_schemes(bit_count):
    return {key: scheme for key, scheme in schemes_for_bits(bit_count).items()
            if scheme.x_states == scheme.tile_columns and scheme.y_states == scheme.tile_rows}


def encoding_scheme(scheme_id=DEFAULT_SCHEME):
    try:
        return SELECTABLE_SCHEMES[scheme_id]
    except KeyError as exc:
        raise ValueError("Unknown image encoding scheme: {}".format(scheme_id)) from exc


def barcode_bits(color, x_shift, y_shift, scheme_id=DEFAULT_SCHEME):
    scheme = encoding_scheme(scheme_id)
    if color not in CHANNELS or not 0 <= x_shift < scheme.x_states or not 0 <= y_shift < scheme.y_states:
        raise ValueError("Color or shift is outside the selected encoding scheme.")
    if scheme.position_codebook:
        code = y_shift * scheme.x_states + x_shift
        return list(map(int, COLOR_BITS[color])) + [(code >> bit) & 1 for bit in range(scheme.bit_count - 3, -1, -1)]
    if scheme.tiled and not scheme.joint:
        position = placement(scheme, x_shift, y_shift)
        bits = list(map(int, COLOR_BITS[color]))
        for value, states in ((position["quadrant_y"], scheme.tile_rows),
                              (position["quadrant_x"], scheme.tile_columns),
                              (position["local_x_shift"], position["local_x_states"]),
                              (position["local_y_shift"], position["local_y_states"])):
            bits.extend((value >> shift) & 1 for shift in range((states-1).bit_length()-1, -1, -1))
        return bits
    if not scheme.joint:
        x_bits, y_bits = (scheme.x_states-1).bit_length(), (scheme.y_states-1).bit_length()
        bits = list(map(int, COLOR_BITS[color]))
        bits += [(x_shift >> shift) & 1 for shift in range(x_bits-1, -1, -1)]
        bits += [(y_shift >> shift) & 1 for shift in range(y_bits-1, -1, -1)]
        if scheme.parity:
            bits.append(sum(bits) % 2)  # Even parity across all nine bits.
        return bits
    code = CHANNELS[color] * scheme.x_states * scheme.y_states + y_shift * scheme.x_states + x_shift
    return [(code >> shift) & 1 for shift in range(scheme.bit_count-1, -1, -1)]


def rotate_point(x, y, span_x, span_y, rotation_degrees):
    """Clockwise rotation in image coordinates, translated to a nonnegative frame."""
    if rotation_degrees == 0:
        return x, y
    if rotation_degrees == 90:
        return span_y - y, x
    if rotation_degrees == 180:
        return span_x - x, span_y - y
    if rotation_degrees == 270:
        return y, span_x - x
    raise ValueError("Origami rotation must be 0, 90, 180, or 270 degrees clockwise.")


def rotate_pixels(pixels, rotation_degrees):
    rotate_point(0, 0, 0, 0, rotation_degrees)
    result = pixels
    for _ in range(rotation_degrees // 90):
        result = [list(row) for row in zip(*result[::-1])]
    return result


def physical_raster_size(scheme_id=DEFAULT_SCHEME, active_image_columns=None):
    """Bounded square-pixel raster with exact physical aspect and resolved bins."""
    scheme = encoding_scheme(scheme_id)
    span = canvas_span(active_image_columns)
    # Each equal-sized tile must have whole raster cells for all of its
    # local shifts. Using the global state count here needlessly explodes
    # raster sizes for uneven joint-code partitions.
    width = span * scheme.tile_columns * math.lcm(*scheme.x_counts)
    height = 8 * scheme.tile_rows * math.lcm(*scheme.y_counts)
    aspect = Fraction(str(EXTENSION_COLUMN_SPACING_NM)) * span * scheme.tile_columns / (Fraction(str(EXTENSION_ROW_SPACING_NM)) * 8 * scheme.tile_rows)
    factor = math.lcm(width // math.gcd(width, aspect.numerator), height // math.gcd(height, aspect.denominator))
    if aspect.numerator * aspect.denominator * factor**2 > 12_000_000:
        # Coprime local counts (e.g. 10 and 11) make a common whole-cell
        # raster enormous. Preserve exact field aspect and resolve the smallest
        # bin with >= 8 pixels instead; physical bin edges remain authoritative.
        factor = math.ceil(max(8*span*scheme.tile_columns*max(scheme.x_counts)/aspect.numerator,
                               8*8*scheme.tile_rows*max(scheme.y_counts)/aspect.denominator))
    return aspect.numerator * factor, aspect.denominator * factor


def physical_reference_raster(raster, scheme_id=DEFAULT_SCHEME, rotation_degrees=0, active_image_columns=None):
    """Render a sample grid using its physical cell sizes, including unequal quadrants."""
    from PIL import Image
    scheme = encoding_scheme(scheme_id)
    size = physical_raster_size(scheme_id, active_image_columns)
    if active_columns(active_image_columns) != IMAGE_COLUMNS:
        from PIL import ImageDraw
        info = geometry(scheme_id, 0, active_image_columns)
        result = Image.new("RGB", size)
        draw = ImageDraw.Draw(result)
        for y in range(raster.height):
            for x in range(raster.width):
                left, right = [round(v / info["field_width_nm"] * size[0]) for v in info["pixel_edges_x_nm"][x:x+2]]
                top, bottom = [round(v / info["field_height_nm"] * size[1]) for v in info["pixel_edges_y_nm"][y:y+2]]
                if right > left and bottom > top:
                    draw.rectangle((left, top, right-1, bottom-1), fill=raster.getpixel((x,y)))
        return result.rotate(-rotation_degrees, expand=True)

    if scheme.tiled:
        result = Image.new("RGB",size)
        for qy,ny in enumerate(scheme.y_counts):
            for qx,nx in enumerate(scheme.x_counts):
                x0,y0 = 6*sum(scheme.x_counts[:qx]),8*sum(scheme.y_counts[:qy])
                patch = raster.crop((x0,y0,x0+6*nx,y0+8*ny))
                left,top = qx*size[0]//scheme.tile_columns,qy*size[1]//scheme.tile_rows
                right,bottom = (qx+1)*size[0]//scheme.tile_columns,(qy+1)*size[1]//scheme.tile_rows
                result.paste(patch.resize((right-left,bottom-top),Image.Resampling.NEAREST),(left,top))
    else:
        result = raster.resize(size,Image.Resampling.NEAREST)
    return result.rotate(-rotation_degrees,expand=True)


def site_name(row, column):
    return "U-Apt_{}-{}_R1".format(row, column)


def axis_placement(counts, shift, site_count, site_pitch):
    quadrant, local = 0, shift
    while local >= counts[quadrant]:
        local -= counts[quadrant]
        quadrant += 1
    pitch = site_pitch/counts[quadrant]
    # Half-cell correction keeps equal-size quadrant boundaries when pitches differ.
    offset = quadrant*site_count*site_pitch + (pitch-site_pitch/counts[0])/2
    return quadrant, local, counts[quadrant], pitch, offset


def sampling_axis(counts, site_count, site_pitch, site_offsets=None):
    if site_offsets is not None:
        centers, edges, weights = [], [0.], []
        first_pitch = site_pitch/counts[0]
        for quadrant, count in enumerate(counts):
            pitch = site_pitch/count
            offset = quadrant*site_count*site_pitch + (pitch-first_pitch)/2
            local = [offset + site*site_pitch + shift*pitch for site in site_offsets for shift in range(count)]
            centers.extend(local)
            edges.extend([(local[i]+local[i+1]+first_pitch)/2 for i in range(len(local)-1)])
            edges.append((quadrant+1)*site_count*site_pitch)
            weights.extend([sum(counts)/(len(counts)*count)]*len(local))
        return centers, edges, weights
    centers, edges = [], [0.]
    weights = []
    first_pitch = site_pitch/counts[0]
    for quadrant, count in enumerate(counts):
        pitch = site_pitch/count
        offset = quadrant*site_count*site_pitch + (pitch-first_pitch)/2
        for index in range(site_count*count):
            centers.append(offset + index*pitch)
            edges.append(quadrant*site_count*site_pitch + (index+1)*pitch)
            weights.append(sum(counts)/(len(counts)*count))
    return centers, edges, weights


def placement(scheme, x, y, active_image_columns=None):
    if scheme.tiled:
        qx,lx,nx,px,ox = axis_placement(scheme.x_counts,x,canvas_span(active_image_columns),EXTENSION_COLUMN_SPACING_NM)
        qy,ly,ny,py,oy = axis_placement(scheme.y_counts,y,8,EXTENSION_ROW_SPACING_NM)
        return {"quadrant": scheme.tile_columns*qy+qx, "quadrant_x": qx, "quadrant_y": qy,
                "local_x_shift": lx, "local_y_shift": ly,
                "local_x_states": nx, "local_y_states": ny,
                "quadrant_offset_x_nm": ox, "quadrant_offset_y_nm": oy,
                "local_x_shift_nm": lx*px, "local_y_shift_nm": ly*py,
                "x_shift_nm": ox+lx*px, "y_shift_nm": oy+ly*py,
                "quadrant_normalization_weight": scheme.x_states*scheme.y_states/(scheme.tile_count*nx*ny)}
    return {"x_shift_nm": x*EXTENSION_COLUMN_SPACING_NM/scheme.x_states,
            "y_shift_nm": y*EXTENSION_ROW_SPACING_NM/scheme.y_states}


def geometry(scheme_id=DEFAULT_SCHEME, rotation_degrees=0, active_image_columns=None, barcode_schema=None, *, validate_overlaps=True):
    scheme = encoding_scheme(scheme_id)
    bit_sites = barcode_site_groups(scheme.bit_count, barcode_schema, validate_overlaps=validate_overlaps)
    columns = active_columns(active_image_columns)
    permanent_sites = fiducial_sites(barcode_schema, columns, scheme.bit_count, validate_overlaps=validate_overlaps)
    span = canvas_span(columns)
    width, height = len(columns) * scheme.x_states, 8 * scheme.y_states
    pixel_x, pixel_y = EXTENSION_COLUMN_SPACING_NM / scheme.x_counts[0], EXTENSION_ROW_SPACING_NM / scheme.y_counts[0]
    rotate_point(0, 0, 0, 0, rotation_degrees)
    portrait = rotation_degrees in (90, 270)
    codebook = []
    for color in CHANNELS:
        for y in range(scheme.y_states):
            for x in range(scheme.x_states):
                bits = barcode_bits(color, x, y, scheme_id)
                codebook.append({"code": int("".join(map(str, bits)), 2), "bits": bits,
                                 "color": color, "x_shift": x, "y_shift": y,
                                 **placement(scheme,x,y,columns)})
    field_x = scheme.tile_columns * span * EXTENSION_COLUMN_SPACING_NM
    field_y = scheme.tile_rows * 8 * EXTENSION_ROW_SPACING_NM
    centers_x, edges_x, weights_x = sampling_axis(scheme.x_counts,span,EXTENSION_COLUMN_SPACING_NM,
        [int(columns[0])-int(c) for c in columns] if len(columns) != span else None)
    centers_y, edges_y, weights_y = sampling_axis(scheme.y_counts,8,EXTENSION_ROW_SPACING_NM)
    last_x, last_y = EXTENSION_COLUMN_SPACING_NM/scheme.x_counts[-1], EXTENSION_ROW_SPACING_NM/scheme.y_counts[-1]
    display_first_x, display_first_y = {0:(pixel_x,pixel_y),90:(last_y,pixel_x),180:(last_x,last_y),270:(pixel_y,last_x)}[rotation_degrees]
    result = {
        "origami_rotation_degrees_clockwise": rotation_degrees,
        "source_image_rotation_degrees": 0,
        "array_coordinate_frame": "origami-local; rotate clockwise by origami_rotation_degrees_clockwise for upright image display",
        "display_columns": height if portrait else width, "display_rows": width if portrait else height,
        "display_pixel_pitch_x_nm": display_first_x,
        "display_pixel_pitch_y_nm": display_first_y,
        "display_field_width_nm": (field_y if portrait else field_x),
        "display_field_height_nm": (field_x if portrait else field_y),
        "encoding_scheme": scheme_id, "encoding_label": scheme.label,
        "encoding_mode": "joint_color_xy" if scheme.joint else "fixed_color_x_y_bits",
        "bit_count": scheme.bit_count, "sites_per_bit": len(bit_sites[0]) if len({len(s) for s in bit_sites}) == 1 else [len(s) for s in bit_sites],
        "parity": "even; bit 9 checks bits 1–8" if scheme.parity else None,
        "bit_order": "bit 1 is the most significant bit; bit {} is the least significant bit".format(scheme.bit_count),
        "code_formula": ("channel_index * x_states * y_states + y_shift * x_states + x_shift"
                         if scheme.joint else ("payload = color_code * {} + x_shift * {} + y_shift; code = (payload << 1) | (popcount(payload) % 2)".format(scheme.x_states*scheme.y_states,scheme.y_states)
                                              if scheme.parity else "color_code * {} + x_shift * {} + y_shift".format(scheme.x_states*scheme.y_states,scheme.y_states))),
        "channel_indices": CHANNELS, "codebook": codebook,
        "reserved_codes": sorted(set(range(1 << scheme.bit_count)) - {entry["code"] for entry in codebook}),
        "x_states": scheme.x_states, "y_states": scheme.y_states, "design_count": scheme.design_count,
        "columns": width, "rows": height,
        "site_pitch_x_nm": EXTENSION_COLUMN_SPACING_NM,
        "site_pitch_y_nm": EXTENSION_ROW_SPACING_NM,
        "pixel_pitch_x_nm": pixel_x, "pixel_pitch_y_nm": pixel_y,
        "field_width_nm": field_x,
        "field_height_nm": field_y,
        "center_span_x_nm": centers_x[-1], "center_span_y_nm": centers_y[-1],
        "pixel_centers_x_nm": centers_x, "pixel_centers_y_nm": centers_y,
        "pixel_edges_x_nm": edges_x, "pixel_edges_y_nm": edges_y,
        "sampling_weights_x": weights_x, "sampling_weights_y": weights_y,
        "nonuniform_sampling": len(columns) != span or len(set(scheme.x_counts))>1 or len(set(scheme.y_counts))>1,
        "parent_scheme": scheme.parent_scheme,
        "tile_columns": scheme.tile_columns, "tile_rows": scheme.tile_rows, "tile_count": scheme.tile_count,
        "image_columns_left_to_right": columns, "active_image_columns": columns,
        "image_sites_per_origami": 8 * len(columns),
        "image_rows_top_to_bottom": ROWS,
        "image_origin_from_C12_H01_nm": [(12 - int(columns[0])) * EXTENSION_COLUMN_SPACING_NM + EXTENSION_EXTRA_COLUMN_GAP_NM, 0],
        "positive_shift_direction": "right / down",
        "always_on_sites": [list(site) for site in permanent_sites],
        "barcode_bits": [{"bit": index + 1, "sites": list(sites)} for index, sites in enumerate(bit_sites)],
        "pixel_mapping": "pixel_x = site_column_index * x_states + x_shift; pixel_y = site_row_index * y_states + y_shift",
    }
    if scheme.tiled:
        result.update(encoding_mode="joint_color_quadrant_local_xy" if scheme.joint else "color_quadrant_local_xy",
            quadrant_count=scheme.tile_count, tile_field_width_nm=span * EXTENSION_COLUMN_SPACING_NM, tile_field_height_nm=40.8,
            local_x_states=scheme.x_counts[0] if len(set(scheme.x_counts))==1 else list(scheme.x_counts),
            local_y_states=scheme.y_counts[0] if len(set(scheme.y_counts))==1 else list(scheme.y_counts),
            quadrant_order=(["top-left", "top-right", "bottom-left", "bottom-right"] if scheme.tile_columns == scheme.tile_rows == 2 else
                            ["tile ({}, {})".format(x,y) for y in range(scheme.tile_rows) for x in range(scheme.tile_columns)]),
            quadrant_sampling=[{"quadrant":scheme.tile_columns*qy+qx,"x_states":nx,"y_states":ny,"design_count":3*nx*ny,
                                "pixel_pitch_x_nm":9.9/nx,"pixel_pitch_y_nm":5.1/ny,
                                "normalization_weight":scheme.x_states*scheme.y_states/(scheme.tile_count*nx*ny)}
                               for qy,ny in enumerate(scheme.y_counts) for qx,nx in enumerate(scheme.x_counts)],
            bit_meaning="Joint {}-bit codebook identifies color, quadrant and local XY shift.".format(scheme.bit_count) if scheme.joint else
                        "bits 1–2 color; then tile Y ({} bits), tile X ({} bits), local X ({} bits), local Y ({} bits)".format(
                            (scheme.tile_rows-1).bit_length(), (scheme.tile_columns-1).bit_length(),
                            (scheme.x_counts[0]-1).bit_length(), (scheme.y_counts[0]-1).bit_length()),
            pixel_mapping="Concatenate quadrant sampling grids; use pixel_centers/edges for nonuniform physical spacing.",
            quadrant_offset_convention="field offset plus half the difference between this quadrant's pitch and the first quadrant's pitch")
        if not scheme.joint:
            result["code_formula"] = "color_code * 16 + ((quadrant_y * {} + quadrant_x) * {} + local_x) * {} + local_y".format(scheme.tile_columns, scheme.x_counts[0], scheme.y_counts[0])
    if not scheme.joint:
        result["color_bits"] = COLOR_BITS
        result["x_shift_bits"] = (scheme.x_counts[0]-1).bit_length()
        result["y_shift_bits"] = (scheme.y_counts[0]-1).bit_length()
        if scheme_id == DEFAULT_SCHEME:
            result["shift_state_bits"] = {str(index): list(bits) for index, bits in enumerate(SHIFT_STATES.values())}
    if scheme.position_codebook:
        result.update(encoding_mode="fixed_color_position_codebook",
                      code_formula="color_code * {} + y_shift * x_states + x_shift".format(1 << (scheme.bit_count - 2)),
                      bit_meaning="Bits 1–2 retain the original color codes; bits 3–{} jointly identify tile and local XY shift.".format(scheme.bit_count))
        result.pop('x_shift_bits', None)
        result.pop('y_shift_bits', None)
    return result


def bin_quadrants(fitted, scheme, rotation_degrees, linear_light):
    import numpy as np
    from PIL import Image
    from color_light import display_rgb
    local = np.rot90(np.asarray(fitted), rotation_degrees//90)
    result = np.zeros((8*scheme.y_states,6*scheme.x_states,3),dtype=float)
    for qy,ny in enumerate(scheme.y_counts):
        for qx,nx in enumerate(scheme.x_counts):
            patch = local[qy*local.shape[0]//scheme.tile_rows:(qy+1)*local.shape[0]//scheme.tile_rows,
                          qx*local.shape[1]//scheme.tile_columns:(qx+1)*local.shape[1]//scheme.tile_columns]
            x0,y0 = 6*sum(scheme.x_counts[:qx]),8*sum(scheme.y_counts[:qy])
            for c in range(3):
                plane = Image.fromarray(patch[:,:,c].astype(np.float32))
                result[y0:y0+8*ny,x0:x0+6*nx,c] = np.asarray(plane.resize((6*nx,8*ny),Image.Resampling.BOX))
    return np.asarray(display_rgb(result,True)).tolist() if linear_light else np.rint(result).astype(int).tolist()



def usable_pixel_score(scheme_id, image_size=(1, 1), fit_mode=FIT_MODES[0], active_image_columns=None):
    """Estimate image-bearing samples after padding, using the best orientation.

    Counts RGB locations once, excluding estimated padding area. This is a
    layout-capacity estimate, not a threshold on naturally black image content.
    Uneven local sampling is approximated by its average density.
    """
    scheme = encoding_scheme(scheme_id)
    columns = active_columns(active_image_columns)
    samples = len(columns) * scheme.x_states * 8 * scheme.y_states
    if fit_mode != FIT_MODES[0]:
        return float(samples)
    image_aspect = image_size[0] / image_size[1]
    field_aspect = (canvas_span(columns) * EXTENSION_COLUMN_SPACING_NM * scheme.tile_columns
                    / (8 * EXTENSION_ROW_SPACING_NM * scheme.tile_rows))
    coverage = max(min(aspect / image_aspect, image_aspect / aspect)
                   for aspect in (field_aspect, 1 / field_aspect))
    return samples * coverage


def scheme_pixel_summary(scheme_id, image_size=(1, 1), fit_mode=FIT_MODES[0], active_image_columns=None):
    scheme = encoding_scheme(scheme_id)
    width = len(active_columns(active_image_columns)) * scheme.x_states
    height = 8 * scheme.y_states
    usable = usable_pixel_score(scheme_id, image_size, fit_mode, active_image_columns)
    return "≈{:,} usable / {:,} total RGB pixels ({}×{}) · {} runs".format(
        round(usable), width * height, width, height, scheme.design_count)


def ranked_schemes(scheme_ids=None, image_size=(1, 1), fit_mode=FIT_MODES[0], active_image_columns=None):
    """Most usable image samples first; preserve existing order for ties."""
    return sorted(SELECTABLE_SCHEMES if scheme_ids is None else scheme_ids,
                  key=lambda key: -round(usable_pixel_score(key, image_size, fit_mode, active_image_columns), 8))


def best_rotation(image_size, scheme_id, active_image_columns=None):
    """Minimize aspect-ratio padding (equivalently, center-crop loss), ties at 0°."""
    image_aspect = image_size[0] / image_size[1]
    scores = []
    for rotation in (0, 90):
        info = geometry(scheme_id, rotation, active_image_columns)
        ratio = info['display_field_width_nm'] / info['display_field_height_nm'] / image_aspect
        scores.append(min(ratio, 1/ratio))  # Fraction occupied with aspect-preserving fit.
    return 90 if scores[1] > scores[0] and not math.isclose(scores[0], scores[1], rel_tol=1e-12) else 0



def best_image_rotation(path, scheme_id=DEFAULT_SCHEME, active_image_columns=None):
    from PIL import Image, ImageOps
    with Image.open(path) as source:
        return best_rotation(ImageOps.exif_transpose(source).size, scheme_id, active_image_columns)


def bin_image(path, mode=FIT_MODES[0], rotation_degrees=0, scheme_id=DEFAULT_SCHEME, linear_light=False, active_image_columns=None):
    """Fit the upright source to a rotated origami field; return origami-local pixels."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise ValueError("RGB import requires Pillow. Run: python -m pip install -r requirements.txt") from exc
    if mode not in FIT_MODES:
        raise ValueError("Unknown image fitting mode.")
    if rotation_degrees not in (0, 90, 180, 270):
        raise ValueError("Rotation must be 0, 90, 180, or 270 degrees clockwise.")
    with Image.open(path) as source:
        oriented = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGBA", oriented.size, (0, 0, 0, 255))
        background.alpha_composite(oriented)
        rgb = background.convert("RGB")
    scheme = encoding_scheme(scheme_id)
    columns = active_columns(active_image_columns)
    width, height = len(columns) * scheme.x_states, 8 * scheme.y_states
    physical_raster = physical_raster_size(scheme_id, columns)
    if rotation_degrees in (90, 270):
        width, height = height, width
        physical_raster = physical_raster[::-1]
    if columns != IMAGE_COLUMNS:
        import numpy as np
        from color_light import fit_linear_source, display_rgb
        if linear_light:
            fitted_array = fit_linear_source(path, mode, physical_raster)
        else:
            fitted = (ImageOps.pad(rgb, physical_raster, method=Image.Resampling.LANCZOS, color="black")
                      if mode == FIT_MODES[0] else ImageOps.fit(rgb, physical_raster, method=Image.Resampling.LANCZOS)
                      if mode == FIT_MODES[1] else rgb.resize(physical_raster, Image.Resampling.LANCZOS))
            fitted_array = np.asarray(fitted)
        local = np.rot90(np.asarray(fitted_array), rotation_degrees//90)
        info = geometry(scheme_id, 0, columns)
        result = np.zeros((info["rows"], info["columns"], 3), dtype=float)
        for channel in range(3):
            plane = Image.fromarray(local[:,:,channel].astype(np.float32))
            for y in range(info["rows"]):
                top, bottom = [v/info["field_height_nm"]*plane.height for v in info["pixel_edges_y_nm"][y:y+2]]
                for x in range(info["columns"]):
                    left, right = [v/info["field_width_nm"]*plane.width for v in info["pixel_edges_x_nm"][x:x+2]]
                    result[y,x,channel] = plane.resize((1,1), Image.Resampling.BOX,
                        box=(left, top, min(right, plane.width), min(bottom, plane.height))).getpixel((0,0))
        return np.asarray(display_rgb(result, True)).tolist() if linear_light else np.rint(result).astype(int).tolist()
    if linear_light:
        from color_light import fit_linear_source, display_rgb
        if scheme.tiled:
            return bin_quadrants(fit_linear_source(path,mode,physical_raster),scheme,rotation_degrees,True)
        binned = display_rgb(fit_linear_source(path, mode, physical_raster, (width,height)), True)
        upright = [[list(binned.getpixel((x,y))) for x in range(width)] for y in range(height)]
        return rotate_pixels(upright, (-rotation_degrees) % 360)
    if mode == FIT_MODES[0]:
        fitted = ImageOps.pad(rgb, physical_raster, method=Image.Resampling.LANCZOS, color="black")
    elif mode == FIT_MODES[1]:
        fitted = ImageOps.fit(rgb, physical_raster, method=Image.Resampling.LANCZOS)
    else:
        fitted = rgb.resize(physical_raster, Image.Resampling.LANCZOS)
    if scheme.tiled:
        return bin_quadrants(fitted,scheme,rotation_degrees,False)
    binned = fitted.resize((width, height), Image.Resampling.BOX)
    upright = [[list(binned.getpixel((x, y))) for x in range(width)] for y in range(height)]
    return rotate_pixels(upright, (-rotation_degrees) % 360)


def synthesis_base_volume_nl(final_volume_ul, target_nm, stock_nm, minimum_transfer_nl=25.0, transfer_increment_nl=25.0):
    """Calculate the exact base volume, independently of the transfer increment."""
    validate_transfer_limits(minimum_transfer_nl, transfer_increment_nl)
    if any(not math.isfinite(value) or value <= 0 for value in (final_volume_ul, target_nm, stock_nm)):
        raise ValueError("Synthesis volume, book-base target, and base stock must be finite and positive.")
    volume = 1000 * final_volume_ul * target_nm / stock_nm
    if not valid_transfer(volume, minimum_transfer_nl, transfer_increment_nl):
        raise ValueError("Requested synthesis needs {:g} nL per base staple; minimum is {:g} nL with {:g} nL increments.".format(volume, minimum_transfer_nl, transfer_increment_nl))
    return round(volume / transfer_increment_nl) * transfer_increment_nl


def rgb_replacement_options(final_volume_ul, target_nm, base_stock_nm, stock_nm, minimum_transfer_nl=25.0, transfer_increment_nl=25.0):
    """Allowed site splits at the requested final concentration and volume."""
    if any(not math.isfinite(value) or value <= 0 for value in (final_volume_ul, target_nm, base_stock_nm, stock_nm)):
        raise ValueError("Final volume, site target, and both stock concentrations must be finite and positive.")
    # A single original-stock droplet's final concentration provides the same
    # reference used by the core split calculator, independent of body targets.
    validate_transfer_limits(minimum_transfer_nl, transfer_increment_nl)
    reference_volume = math.ceil(minimum_transfer_nl / transfer_increment_nl) * transfer_increment_nl
    base_droplet_nm = base_stock_nm * reference_volume / 1000 / final_volume_ul
    fractions = replacement_fractions(reference_volume, target_nm, base_droplet_nm, base_stock_nm / stock_nm,
                                     minimum_transfer_nl, transfer_increment_nl)
    return dict(fractions=fractions, full_original_nl=1000 * final_volume_ul * target_nm / base_stock_nm,
                full_replacement_nl=1000 * final_volume_ul * target_nm / stock_nm)


def rgb_transfer_grid(base_volume, target_nm, base_target_nm=50.0, base_stock_nm=200000.0, stock_nm=200000.0, minimum_transfer_nl=25.0, transfer_increment_nl=25.0):
    """Use the same molar split and droplet limits as manual replacements."""
    for value in (base_stock_nm, stock_nm):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Stock concentrations must be finite positive numbers.")
    options = replacement_fractions(base_volume, target_nm, base_target_nm, base_stock_nm / stock_nm, minimum_transfer_nl, transfer_increment_nl)
    if len(options) < 2 or not math.isclose(options[0], 0.0) or not math.isclose(options[-1], 1.0):
        raise ValueError("RGB requires both 0% and 100% replacement for binary barcodes. "
                         "The RGB target and synthesis settings do not permit both under the configured transfer limits; "
                         "increase the synthesis volume or adjust the RGB target or stock concentrations.")
    # Optimization uses a uniform search grid; admissible fractions may have gaps
    # when the minimum exceeds the increment. Final assignments are projected.
    steps = math.lcm(*(Fraction(value).limit_denominator(1000000).denominator for value in options))
    original = base_volume * target_nm / base_target_nm
    return dict(fraction_steps=steps, allowed_fractions=options, minimum_transfer_nl=minimum_transfer_nl,
                transfer_increment_nl=transfer_increment_nl, full_original_nl=original,
                full_replacement_nl=original * base_stock_nm / stock_nm)


def transfer_fraction_steps(design, site):
    """Legacy snapshots used equal stocks and encoded steps in total volume."""
    return design.get("fraction_steps", (site["replacement_nl"] + site["original_nl"]) / 25)


def set_site_fraction(design, site, fraction):
    if design.get("allowed_fractions"):
        fraction = min(design["allowed_fractions"], key=lambda value: (abs(value-fraction), -value))
    increment = design.get("transfer_increment_nl", 25)
    total = site["replacement_nl"] + site["original_nl"]
    original = design.get("full_original_nl", total)
    replacement = design.get("full_replacement_nl", total)
    site.update(replacement_fraction=float(fraction),
                replacement_nl=round(replacement * fraction / increment) * increment,
                original_nl=round(original * (1 - fraction) / increment) * increment)
    design["site_fractions"][site["name"]] = float(fraction)


def compile_designs(pixels, base_volume, multipliers, scheme_id=DEFAULT_SCHEME, rotation_degrees=0, linear_light=False, *,
                    plate_defaults_nm=None, base_desired_concentration_nm=50.0,
                    base_concentration_nm=200000.0, source_concentrations_nm=None, plate_relative_transfers=False, active_image_columns=None, barcode_schema=None,
                    minimum_transfer_nl=25.0, transfer_increment_nl=25.0, validate_overlaps=True):
    """Deinterleave each RGB channel using the selected barcode codebook."""
    scheme = encoding_scheme(scheme_id)
    bit_sites = barcode_site_groups(scheme.bit_count, barcode_schema, validate_overlaps=validate_overlaps)
    columns = active_columns(active_image_columns)
    permanent_sites = fiducial_sites(barcode_schema, columns, scheme.bit_count, validate_overlaps=validate_overlaps)
    width, height = len(columns) * scheme.x_states, 8 * scheme.y_states
    pixel_x, pixel_y = EXTENSION_COLUMN_SPACING_NM / scheme.x_counts[0], EXTENSION_ROW_SPACING_NM / scheme.y_counts[0]
    if len(pixels) != height or any(len(row) != width for row in pixels):
        raise ValueError("RGB image must contain exactly {} columns and {} rows.".format(width, height))
    for row in pixels:
        for pixel in row:
            if len(pixel) != 3 or any(not isinstance(value, int) or not 0 <= value <= 255 for value in pixel):
                raise ValueError("Each pixel must have three integer RGB values from 0 to 255.")
    rotate_point(0, 0, 0, 0, rotation_degrees)
    info = geometry(scheme_id,rotation_degrees,columns,barcode_schema, validate_overlaps=validate_overlaps)
    from color_light import srgb_to_linear
    intensity_lut = srgb_to_linear([v/255 for v in range(256)]) if linear_light else [v/255 for v in range(256)]
    replacement_fractions(base_volume, minimum_transfer_nl=minimum_transfer_nl, transfer_increment_nl=transfer_increment_nl)  # Validate the shared base droplet volume independently.
    designs = []
    for color, channel in CHANNELS.items():
        # Explicit plate defaults are authoritative; multipliers are legacy-only.
        multiplier = multipliers[color] if plate_defaults_nm is None else 1.0
        target = base_desired_concentration_nm * multiplier if plate_defaults_nm is None else plate_defaults_nm[color]
        stock = (source_concentrations_nm or {}).get(color, 200000.0)
        reference_target = target if plate_relative_transfers else base_desired_concentration_nm
        grid = rgb_transfer_grid(base_volume, target, reference_target, base_concentration_nm, stock, minimum_transfer_nl, transfer_increment_nl)
        concentrations = {} if plate_defaults_nm is None else dict(
            plate_default_nm=target, desired_concentration_nm=target,
            base_desired_concentration_nm=reference_target, plate_relative_transfers=plate_relative_transfers,
            base_concentration_nm=base_concentration_nm, source_concentration_nm=stock)
        for y_shift in range(scheme.y_states):
            for x_shift in range(scheme.x_states):
                bits = barcode_bits(color, x_shift, y_shift, scheme_id)
                position = placement(scheme,x_shift,y_shift,columns)
                fractions = {site_name(*site): 1.0 for site in permanent_sites}
                for state, sites in zip(bits, bit_sites):
                    for row, column in sites:
                        fractions[site_name(row, column)] = float(state)
                sites = []
                for row_index, row in enumerate(ROWS):
                    for column_index, column in enumerate(columns):
                        x, y = column_index * scheme.x_states + x_shift, row_index * scheme.y_states + y_shift
                        if scheme.tiled:
                            x = len(columns)*sum(scheme.x_counts[:position["quadrant_x"]]) + column_index*position["local_x_states"] + position["local_x_shift"]
                            y = 8*sum(scheme.y_counts[:position["quadrant_y"]]) + row_index*position["local_y_states"] + position["local_y_shift"]
                        value = pixels[y][x][channel]
                        fraction = min(grid["allowed_fractions"], key=lambda f: (abs(f-float(intensity_lut[value])), -f))
                        name = site_name(row, column)
                        fractions[name] = fraction
                        display_x, display_y = rotate_point(x, y, width - 1, height - 1, rotation_degrees)
                        x_nm,y_nm = info["pixel_centers_x_nm"][x],info["pixel_centers_y_nm"][y]
                        display_x_nm, display_y_nm = rotate_point(x_nm,y_nm,
                            info["center_span_x_nm"],info["center_span_y_nm"],rotation_degrees)
                        sites.append({
                            "display_pixel_x": display_x, "display_pixel_y": display_y,
                            "display_x_nm": display_x_nm, "display_y_nm": display_y_nm,
                            "name": name, "row": row, "column": column,
                            "pixel_x": x, "pixel_y": y, "rgb": pixels[y][x][:],
                            "x_nm": x_nm, "y_nm": y_nm,
                            "channel_value": value, "replacement_fraction": fraction,
                            "replacement_nl": round(grid["full_replacement_nl"] * fraction / transfer_increment_nl) * transfer_increment_nl,
                            "original_nl": round(grid["full_original_nl"] * (1 - fraction) / transfer_increment_nl) * transfer_increment_nl,
                        })
                suffix = "" if scheme_id == DEFAULT_SCHEME else "_{}x{}".format(scheme.x_states,scheme.y_states)
                design_name = ("{}{}_Q{}_X{}_Y{}".format(color.replace(" ","_"),suffix,position["quadrant"],position["local_x_shift"],position["local_y_shift"])
                               if scheme.tiled else "{}{}_X{}_Y{}".format(color.replace(" ","_"),suffix,x_shift,y_shift))
                designs.append({
                    "name": design_name,
                    "origami_rotation_degrees_clockwise": rotation_degrees,
                    "linear_light": linear_light, "encoding_scheme": scheme_id, "active_image_columns": columns[:],
                    "active_canvas_offset_nm": [(6-int(columns[0]))*EXTENSION_COLUMN_SPACING_NM, 0], "barcode_code": int("".join(map(str, bits)), 2),
                    "color": color, "channel": channel, "x_shift": x_shift, "y_shift": y_shift,
                    **position,
                    "always_on_sites": [list(site) for site in permanent_sites],
                    "bits": [int(bit) for bit in bits], "barcode_sites": [[list(site) for site in sites] for sites in bit_sites], "base_volume_nl": base_volume,
                    **grid, **concentrations,
                    "excess_multiplier": multiplier, "site_fractions": fractions, "image_sites": sites,
                })
    return designs


def reconstruct(designs):
    scheme_id = designs[0].get("encoding_scheme", DEFAULT_SCHEME) if designs else DEFAULT_SCHEME
    if any(design.get("encoding_scheme", DEFAULT_SCHEME) != scheme_id for design in designs):
        raise ValueError("Cannot reconstruct designs from different encoding schemes together.")
    scheme = encoding_scheme(scheme_id)
    columns = active_columns(designs[0].get("active_image_columns") if designs else None)
    if any(active_columns(d.get("active_image_columns")) != columns for d in designs):
        raise ValueError("Cannot reconstruct different active canvases together.")
    pixels = [[[0, 0, 0] for _ in range(len(columns) * scheme.x_states)] for _ in range(8 * scheme.y_states)]
    for design in designs:
        for site in design["image_sites"]:
            pixels[site["pixel_y"]][site["pixel_x"]][design["channel"]] = round(site["replacement_fraction"] * 255)
    return pixels


def selection_panel(design):
    fractions = design["site_fractions"]
    selected = [(row, column) for row in ROWS for column in ("{:02d}".format(c) for c in range(12, 0, -1))
                if fractions.get(site_name(row, column), 0) > 0]
    return {
        "origami_rotation_degrees_clockwise": design.get("origami_rotation_degrees_clockwise", 0),
        "encoding_scheme": design.get("encoding_scheme", DEFAULT_SCHEME),
        "group": design["color"], "label": design["name"], "rows": ROWS[:],
        "columns": ["{:02d}".format(c) for c in range(12, 0, -1)],
        "active": [(row, "{:02d}".format(c)) for row in ROWS for c in range(1, 13)],
        "selected": selected, "selected_names": [site_name(row, column) for row, column in selected],
        "site_fractions": dict(fractions), "x_shift": design["x_shift"], "y_shift": design["y_shift"],
        "site_concentrations_nm": ({name: design["desired_concentration_nm"] for name in fractions}
                                   if "desired_concentration_nm" in design else {}),
        "spacing_x_nm": EXTENSION_COLUMN_SPACING_NM, "spacing_y_nm": EXTENSION_ROW_SPACING_NM,
        "mirror_columns": True,
    }


def prepare_runs(designs, base_path, sources, available_wells, settings, mixing, source_concentrations_nm=None, biotin_source=None, biotin_target_nm=None):
    """Allocate separate wells and validate every recipe before returning any runs."""
    for design in designs:
        permanent = {tuple(site) for site in design.get("always_on_sites", [])}
        barcode = {tuple(site) for group in design.get("barcode_sites", []) for site in group}
        analog = {(site["row"], site["column"]) for site in design.get("image_sites", [])}
        if permanent & (barcode | analog):
            raise ValueError("Always-on sites overlap barcode bits or the active analog canvas. Update the barcode layout before preparing runs.")
    source_concentrations_nm = source_concentrations_nm or {}
    remaining = list(available_wells)
    if len(set(remaining)) != len(remaining):
        raise ValueError("Destination wells must be unique.")
    if len(remaining) < len(designs):
        raise ValueError("This batch needs at least {} destination wells; only {} are available on the selected plate.".format(len(designs),len(remaining)))
    runs = []
    biotin_rows = []
    if biotin_source is not None:
        if biotin_target_nm is not None and (not math.isfinite(biotin_target_nm) or biotin_target_nm <= 0):
            raise ValueError("Biotin target must be a finite, positive concentration in nM.")
        biotin_path, biotin_plate, biotin_stock = biotin_source
        biotin_rows, _ = parse_source2(Path(biotin_path))
        expected = {"D-Biotin_{}-{}".format(row, column) for row in ("H01", "H15") for column in ("01", "05", "08", "12")}
        if len(biotin_rows) != 8 or {row["Name"] for row in biotin_rows} != expected:
            raise ValueError("D-Biotin source must contain all eight PAINT P1 D-Biotin staples exactly once.")
        if len({row["Well"] for row in biotin_rows}) != 8 or len({row["Replace Well"] for row in biotin_rows}) != 8:
            raise ValueError("D-Biotin source wells and logical replacement sites must be unique.")
    settings = dict(settings)
    settings.setdefault("base_concentration_nm", mixing["staple"][0])
    settings.setdefault("base_desired_concentration_nm", mixing["desired"][1])
    base_rows, base_plate = parse_source1(Path(base_path), settings["hinge_type"])
    source_rows = {}
    for design in designs:
        source_path, source_plate = sources[design["color"]]
        if source_path not in source_rows:
            source_rows[source_path] = parse_source2(Path(source_path))[0]
        run_settings = dict(settings)
        run_mixing = {key: list(value) for key, value in mixing.items()}
        if design.get("plate_relative_transfers"):
            run_settings.update(base_concentration_nm=design["base_concentration_nm"],
                                base_desired_concentration_nm=design["desired_concentration_nm"])
            run_mixing["staple"][0] = design["base_concentration_nm"]
            run_mixing["desired"][1] = design["desired_concentration_nm"]
            # The dilute base can support a smaller final recipe at the same
            # transfer volume. Use only the material actually dispensed.
            available_final_ul = (design["base_volume_nl"] * run_settings["transfers_per_source"]
                                  * design["base_concentration_nm"] / design["desired_concentration_nm"] / 1000)
            run_mixing["desired"][0] = min(run_mixing["desired"][0], available_final_ul)
        if "desired_concentration_nm" in design:
            for key in ("base_concentration_nm", "base_desired_concentration_nm"):
                if not math.isclose(design[key], run_settings[key]):
                    raise ValueError("Base concentration settings changed; regenerate the RGB assignments.")
            if design["color"] in source_concentrations_nm and not math.isclose(
                    design["source_concentration_nm"], source_concentrations_nm[design["color"]]):
                raise ValueError("Source concentration changed; regenerate the RGB assignments.")
        selection = ReplacementSelection(Path(source_path), list(design["site_fractions"]), source_plate,
                                         desired_concentration_nm=design.get("desired_concentration_nm", settings["base_desired_concentration_nm"] * design["excess_multiplier"]),
                                         concentration_nm=design.get("source_concentration_nm", source_concentrations_nm.get(design["color"], 200000.0)),
                                         site_fractions=design["site_fractions"])
        try:
            selections = [selection]
            if biotin_rows:
                biotin_target = run_settings["base_desired_concentration_nm"] if biotin_target_nm is None else biotin_target_nm
                selections.append(ReplacementSelection(Path(biotin_path), [row["Name"] for row in biotin_rows],
                    biotin_plate, desired_concentration_nm=biotin_target,
                    concentration_nm=biotin_stock))
            picklist = generate_picklist(Path(base_path), selections, remaining,
                                        transfer_volume_nl=design["base_volume_nl"], **run_settings)
            paint_rows = [row for row in source_rows[source_path] if row["Name"] in design["site_fractions"]]
            paint_sites = {row["Replace Well"]: row["Name"] for row in paint_rows}
            site_locations = {site_name(helix, f"{row:02d}"): f"{helix}, {row:02d}"
                              for helix in ROWS for row in range(1, 13)}
            site_types = {site["name"]: "image site" for site in design["image_sites"]}
            site_types.update({site_name(*site): "barcode site"
                               for group in design["barcode_sites"] for site in group})
            site_types.update({site_name(*site): "fiducial site"
                               for site in design.get("always_on_sites", [])})
            def paint_function(role, name):
                if role == "PAINT extension":
                    return "{} ({}; {})".format(role, site_locations[name], site_types[name])
                return "{} ({})".format(role, site_locations[name])
            roles = {(base_plate, row["Well"]): paint_function("body-PAINT competitor", paint_sites[row["Replacement Well"]]) for row in base_rows
                     if row["Replacement Well"] in paint_sites}
            roles.update({(source_plate, row["Well"]): paint_function("PAINT extension", row["Name"]) for row in paint_rows})
            if biotin_rows:
                roles.update({(biotin_plate, row["Well"]): "biotin" for row in biotin_rows})
            # Use logical replacement keys, never relocated physical source wells,
            # to locate retained body staples on the PAINT grid.
            locations_by_key = {}
            for row in source_rows[source_path] + biotin_rows:
                match = re.search(r"(H\d{2})-(\d{2})", row["Name"])
                if match:
                    locations_by_key[row["Replace Well"]] = list(match.groups())
            source_locations = {(base_plate, row["Well"]): locations_by_key[row["Replacement Well"]]
                                for row in base_rows if row["Replacement Well"] in locations_by_key}
            for plate, rows in ((source_plate, paint_rows), (biotin_plate if biotin_rows else "", biotin_rows)):
                for row in rows:
                    if row["Replace Well"] in locations_by_key:
                        source_locations[plate, row["Well"]] = locations_by_key[row["Replace Well"]]
            for transfer in picklist:
                transfer["site_location"] = source_locations.get((transfer["Source Plate Name"], transfer["Source Well"]))
                transfer["function"] = roles.get((transfer["Source Plate Name"], transfer["Source Well"]), "standard body")
            occupied = list(dict.fromkeys(row["Destination Well"] for row in picklist))
            remaining = [well for well in remaining if well not in occupied]
            total = sum(row["Transfer Volume"] for row in picklist)
            effective_staples = total / (design["base_volume_nl"] * settings["transfers_per_source"])
            recipe = calculate_mixing_volumes(number_of_staples=effective_staples, **run_mixing)
            if recipe[0]["Volume_uL"] > total / 1000 + 1e-9:
                raise ValueError("Requested staple mix exceeds the available picklist volume; adjust RGB mixing settings.")
        except ValueError as exc:
            raise ValueError("{}: {}".format(design["name"], exc)) from exc
        runs.append({"run_name": design["name"], "design_names": [name for name, fraction in design["site_fractions"].items() if fraction > 0],
                     "hinge_type": settings["hinge_type"], "selection_panels": [selection_panel(design)],
                     "destination_plate_name": settings["destination_plate_name"], "destination_wells": occupied,
                     "transfer_count": len(picklist), "picklist": picklist, "mixing_recipe": recipe,
                     "image_design": design})
        if biotin_rows:
            runs[-1]["design_names"].extend(row["Name"] for row in biotin_rows)
            positions = [(row, column) for row in ("H01", "H15") for column in ("12", "08", "05", "01")]
            runs[-1]["selection_panels"].append({"group": "D-Biotin", "label": "D-Biotin — {:g} nM target".format(biotin_target),
                "rows": ["H01", "H15"], "columns": ["12", "08", "05", "01"],
                "active": positions, "selected": positions, "selected_names": [row["Name"] for row in biotin_rows],
                "site_fractions": {row["Name"]: 1.0 for row in biotin_rows},
                "site_concentrations_nm": {row["Name"]: biotin_target for row in biotin_rows}})
            runs[-1]["d_biotin"] = {"source_path": str(biotin_path), "source_plate": biotin_plate,
                "stock_nm": biotin_stock, "target_nm": biotin_target,
                "staples": [dict(row) for row in biotin_rows]}
    return runs
