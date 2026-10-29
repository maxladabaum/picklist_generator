"""Scale-independent shrinkage toward direct, quantized image assignments."""
import math

DEFAULT_REGULARIZATION = 0.05


def validate_regularization(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('Regularization strength must be finite and nonnegative; 0 disables it.')
    return value


def regularization_weight(strength, raster_pixels, site_count):
    # Solvers minimize sums per channel. Normalize to image MSE + lambda *
    # mean squared fraction change so raster resolution/site count do not set lambda.
    return validate_regularization(strength) * raster_pixels / site_count
