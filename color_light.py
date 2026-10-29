"""sRGB display values versus additive linear-light signal."""
import numpy as np
from PIL import Image, ImageOps


def srgb_to_linear(values):
    values = np.asarray(values, dtype=float)
    return np.where(values <= .04045, values / 12.92, ((values + .055) / 1.055)**2.4)


def linear_to_srgb(values):
    values = np.clip(np.asarray(values, dtype=float), 0, 1)
    return np.where(values <= .0031308, 12.92 * values, 1.055 * values**(1/2.4) - .055)


def display_rgb(linear, linear_light):
    values = linear_to_srgb(linear) if linear_light else np.clip(linear, 0, 1)
    return Image.fromarray(np.rint(values * 255).astype(np.uint8))


def fit_linear_source(path, mode, size, bin_size=None):
    """Decode before alpha compositing/resampling. Assume input RGB is sRGB."""
    with Image.open(path) as source:
        rgba = np.asarray(ImageOps.exif_transpose(source).convert('RGBA'), dtype=float) / 255
    signal = srgb_to_linear(rgba[:,:,:3]) * rgba[:,:,3:4]
    channels = []
    for channel in range(3):
        plane = Image.fromarray(signal[:,:,channel].astype(np.float32))
        if mode == 'Fit with black padding':
            plane = ImageOps.pad(plane, size, method=Image.Resampling.LANCZOS, color=0)
        elif mode == 'Center crop':
            plane = ImageOps.fit(plane, size, method=Image.Resampling.LANCZOS)
        elif mode == 'Stretch':
            plane = plane.resize(size, Image.Resampling.LANCZOS)
        else:
            raise ValueError('Unknown image fitting mode.')
        if bin_size is not None:
            plane = plane.resize(bin_size, Image.Resampling.BOX)
        channels.append(np.asarray(plane))
    return np.clip(np.stack(channels,axis=2),0,1)


def abundance_scale(scheme_id, normalize_designs):
    if not normalize_designs:
        return 1.0
    from image_encoding import encoding_scheme
    scheme = encoding_scheme(scheme_id)
    # Fixed total weight relative to the original 16 shifts/color, not auto-exposure.
    return 16.0 / (scheme.x_states * scheme.y_states)


def canvas_area_gain(scheme_id, compensate_area):
    """Exposure compensation for a larger virtual canvas; does not add photons."""
    from image_encoding import encoding_scheme
    return float(encoding_scheme(scheme_id).tile_count) if compensate_area else 1.0


def quadrant_weight(design, normalize_designs):
    """Balance signal among equal-area quadrants with different sampling counts."""
    return design.get("quadrant_normalization_weight", 1.0) if normalize_designs else 1.0
