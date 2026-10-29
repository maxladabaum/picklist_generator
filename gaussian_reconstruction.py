"""Expected additive Gaussian reconstruction of independently encoded image sites."""
from __future__ import annotations

from dataclasses import dataclass
import math

from color_light import display_rgb, abundance_scale, canvas_area_gain, quadrant_weight
from image_encoding import DEFAULT_SCHEME, PIXEL_X_NM, PIXEL_Y_NM, geometry

DEFAULT_FWHM_NM = 7.5
DEFAULT_EXPOSURE = 1.0
# One common reference for every scheme/channel, never an image-dependent maximum.
REFERENCE_AREA_NM2 = PIXEL_X_NM * PIXEL_Y_NM


@dataclass
class GaussianReconstruction:
    density: object  # Upright float64 array, [y, x, RGB], relative signal / nm².
    rgb: object  # Pillow image, rendered with shared exposure and clipping.
    metadata: dict


def render_gaussian_overlay(designs, fwhm_nm=DEFAULT_FWHM_NM, exposure=DEFAULT_EXPOSURE, linear_light=False, normalize_designs=False, compensate_area=False):
    """Sum unit-integral, isotropic Gaussians at physical, shifted site positions.

    Integrated signal is proportional to replacement fraction. Assumes equal
    representation of every design and equal channel brightness. Barcode sites
    are excluded. Gaussian tails are cropped at the image-field boundaries.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Gaussian previews require NumPy and Pillow. Install requirements.txt.") from exc
    if not math.isfinite(fwhm_nm) or fwhm_nm <= 0:
        raise ValueError("Gaussian FWHM must be a finite positive number in nm.")
    if not math.isfinite(exposure) or exposure <= 0:
        raise ValueError("Exposure must be a finite positive number.")
    if not designs:
        raise ValueError("At least one encoded design is required for reconstruction.")
    scheme_id = designs[0].get("encoding_scheme", DEFAULT_SCHEME)
    rotation = designs[0].get("origami_rotation_degrees_clockwise", 0)
    if any(item.get("encoding_scheme", DEFAULT_SCHEME) != scheme_id or
           item.get("origami_rotation_degrees_clockwise", 0) != rotation for item in designs):
        raise ValueError("Reconstruct one encoding scheme and origami orientation at a time.")
    info = geometry(scheme_id, rotation, designs[0].get('active_image_columns'))
    sigma = fwhm_nm / math.sqrt(8 * math.log(2))
    # Resolve narrow Gaussians while bounding memory for accidental tiny inputs.
    min_pixel_nm = math.sqrt(info['field_width_nm'] * info['field_height_nm'] / 1_900_000)
    requested_pixel_nm = max(min(0.15, fwhm_nm / 4), min_pixel_nm)
    if requested_pixel_nm > fwhm_nm / 4:
        raise ValueError('Gaussian width is too small for this field. Choose FWHM of at least {:.3g} nm.'.format(4*min_pixel_nm))
    width = math.ceil(info["field_width_nm"] / requested_pixel_nm - 1e-9)
    height = math.ceil(info["field_height_nm"] / requested_pixel_nm - 1e-9)
    if width * height > 2_000_000:
        raise ValueError("Gaussian raster exceeds the preview size limit for this field.")
    dx, dy = info["field_width_nm"] / width, info["field_height_nm"] / height
    xmin, ymin = -info["pixel_pitch_x_nm"] / 2, -info["pixel_pitch_y_nm"] / 2
    xs = xmin + (np.arange(width) + .5) * dx
    ys = ymin + (np.arange(height) + .5) * dy
    sites = []
    for design in designs:
        channel = design["channel"]
        if channel not in (0, 1, 2):
            raise ValueError("Each design must specify an RGB channel.")
        for site in design["image_sites"]:
            fraction, x, y = site.get("effective_brightness", site["replacement_fraction"]), site["x_nm"], site["y_nm"]
            if not all(math.isfinite(value) for value in (fraction, x, y)) or not 0 <= fraction or ("effective_brightness" not in site and fraction > 1):
                raise ValueError("Image-site coordinates and replacement fractions must be finite and valid.")
            if fraction > 0:
                sites.append((x, y, channel, fraction * quadrant_weight(design, normalize_designs)))
    density = np.zeros((height, width, 3), dtype=np.float64)
    if sites:
        source_x = sorted({site[0] for site in sites})
        source_y = sorted({site[1] for site in sites})
        index_x, index_y = {x:i for i,x in enumerate(source_x)}, {y:i for i,y in enumerate(source_y)}
        weights = np.zeros((len(source_y), len(source_x), 3), dtype=np.float64)
        for x, y, channel, fraction in sites:
            weights[index_y[y], index_x[x], channel] += fraction
        kernel_x = np.exp(-.5 * ((xs[:, None] - np.asarray(source_x)[None, :]) / sigma)**2)
        kernel_y = np.exp(-.5 * ((ys[:, None] - np.asarray(source_y)[None, :]) / sigma)**2)
        for channel in range(3):
            density[:, :, channel] = (kernel_y @ weights[:, :, channel] @ kernel_x.T) / (2 * math.pi * sigma**2)
    density = np.rot90(density, -(rotation // 90)).copy()
    scale = abundance_scale(scheme_id, normalize_designs)
    area_gain = canvas_area_gain(scheme_id, compensate_area)
    gain = REFERENCE_AREA_NM2 * exposure * scale * area_gain
    linear_rgb = density * gain
    rgb = display_rgb(linear_rgb, linear_light)
    portrait = rotation in (90, 270)
    metadata = {
        "model": "sum_of_unit_integral_isotropic_gaussians",
        "linear_light": linear_light, "normalize_designs": normalize_designs,
        "compensate_area": compensate_area, "canvas_area_gain": area_gain,
        "design_abundance_scale": scale, "linear_rgb_gain": gain,
        "display_encoding": "sRGB" if linear_light else "legacy direct RGB",
        "fwhm_nm": fwhm_nm, "sigma_nm": sigma, "exposure": exposure,
        "quadrant_balance_applied": normalize_designs and any(d.get("quadrant_normalization_weight",1) != 1 for d in designs),
        "signal_weight": "replacement fraction; with normalization, unequal quadrant sampling is balanced by quadrant_normalization_weight",
        "barcode_sites_included": False,
        "normalization": "Signal = density * linear_rgb_gain; clip then encode for display. Fixed total design weight when normalize_designs is true; no image-dependent scaling.",
        "reference_area_nm2": REFERENCE_AREA_NM2,
        "reference": "mean density of a full-on original 4x4 sampling lattice",
        "density_units": "relative integrated site signal per nm^2",
        "coordinate_frame": "upright image",
        "origami_rotation_degrees_clockwise": rotation,
        "encoding_scheme": scheme_id, "design_count": len(designs), "emitting_site_count": len(sites),
        "raster_columns": int(density.shape[1]), "raster_rows": int(density.shape[0]),
        "raster_pixel_x_nm": dy if portrait else dx, "raster_pixel_y_nm": dx if portrait else dy,
        "field_width_nm": info["display_field_width_nm"], "field_height_nm": info["display_field_height_nm"],
        "first_sample_x_nm": -(info["display_pixel_pitch_x_nm"] / 2) + (dy if portrait else dx) / 2,
        "first_sample_y_nm": -(info["display_pixel_pitch_y_nm"] / 2) + (dx if portrait else dy) / 2,
        "edge_handling": "Gaussian tails outside the image field are cropped, not wrapped or renormalized",
        "clipped_pixel_fraction": float(np.mean(np.any(linear_rgb > 1 + 1e-6, axis=2))),
    }
    return GaussianReconstruction(density, rgb, metadata)
