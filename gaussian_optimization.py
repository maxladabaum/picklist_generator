"""Bounded, droplet-quantized inverse design for the Gaussian forward model."""
from copy import deepcopy
import math
import numpy as np
from PIL import Image, ImageOps
from color_light import fit_linear_source, abundance_scale, canvas_area_gain
from image_encoding import FIT_MODES, geometry, transfer_fraction_steps, set_site_fraction
from optimization_regularization import regularization_weight
from gaussian_reconstruction import render_gaussian_overlay, REFERENCE_AREA_NM2


def fitted_target(path, mode, size, linear_light=False):
    """The upright source, fitted to the physical reconstruction raster."""
    if linear_light:
        return fit_linear_source(path, mode, size)
    with Image.open(path) as source:
        rgba = ImageOps.exif_transpose(source).convert('RGBA')
        background = Image.new('RGBA', rgba.size, (0, 0, 0, 255))
        background.alpha_composite(rgba)
        image = background.convert('RGB')
    if mode == FIT_MODES[0]:
        image = ImageOps.pad(image, size, method=Image.Resampling.LANCZOS, color='black')
    elif mode == FIT_MODES[1]:
        image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    elif mode == FIT_MODES[2]:
        image = image.resize(size, Image.Resampling.LANCZOS)
    else:
        raise ValueError('Unknown image fitting mode.')
    return np.asarray(image, dtype=float) / 255


def optimize_designs(designs, target, fwhm_nm=7.5, exposure=1.0, linear_light=False, normalize_designs=False, compensate_area=False, regularization_strength=0.0):
    """Minimize unclipped RGB squared error; retain baseline if refinement is worse.

    Continuous projected gradient followed by quantized coordinate descent from
    both the rounded solution and baseline. This is a local, not global, optimum.
    Input designs and their binary barcode fractions are never mutated.
    """
    baseline = render_gaussian_overlay(designs, fwhm_nm, exposure, linear_light, normalize_designs, compensate_area)
    target = np.asarray(target, dtype=float)
    if target.shape != baseline.density.shape or not np.all(np.isfinite(target)) or np.any((target < 0) | (target > 1)):
        raise ValueError('Target must be finite RGB in [0,1] on the reconstruction raster.')
    rotation = designs[0].get('origami_rotation_degrees_clockwise', 0)
    info = geometry(designs[0]['encoding_scheme'], rotation, designs[0].get('active_image_columns'))
    local_target = np.rot90(target, rotation // 90)
    height, width = local_target.shape[:2]
    nx, ny = info['columns'], info['rows']
    alpha = regularization_weight(regularization_strength, height*width, nx*ny)
    sigma = fwhm_nm / math.sqrt(8 * math.log(2))
    xs = -info['pixel_pitch_x_nm']/2 + (np.arange(width)+.5)*info['field_width_nm']/width
    ys = -info['pixel_pitch_y_nm']/2 + (np.arange(height)+.5)*info['field_height_nm']/height
    kx = np.exp(-.5*((xs[:,None]-np.asarray(info['pixel_centers_x_nm'])[None,:])/sigma)**2)
    ky = np.exp(-.5*((ys[:,None]-np.asarray(info['pixel_centers_y_nm'])[None,:])/sigma)**2)
    if normalize_designs:
        kx *= np.asarray(info['sampling_weights_x'])[None,:]
        ky *= np.asarray(info['sampling_weights_y'])[None,:]
    signal_gain = REFERENCE_AREA_NM2 * exposure * abundance_scale(designs[0]["encoding_scheme"], normalize_designs) * canvas_area_gain(designs[0]["encoding_scheme"], compensate_area)
    gain = signal_gain / (2*math.pi*sigma**2)
    gx, gy = kx.T @ kx, ky.T @ ky
    lipschitz = gain**2 * np.linalg.eigvalsh(gx)[-1] * np.linalg.eigvalsh(gy)[-1] + alpha
    diagonal = gain**2 * np.outer(np.diag(gy), np.diag(gx)) + alpha
    result = deepcopy(designs)
    for channel in range(3):
        weights = np.zeros((ny,nx))
        steps = np.zeros((ny,nx))
        seen = set()
        for design in result:
            if design['channel'] != channel:
                continue
            for site in design['image_sites']:
                y,x = site['pixel_y'],site['pixel_x']
                if (y,x) in seen:
                    raise ValueError('Optimization requires one design assignment per channel and pixel.')
                seen.add((y,x))
                weights[y,x] = site['replacement_fraction']
                steps[y,x] = transfer_fraction_steps(design, site)
        if len(seen) != nx*ny or np.any(steps < 1) or np.any(steps != np.rint(steps)):
            raise ValueError('Optimization requires a complete grid and integral fraction steps.')
        b = gain * ky.T @ local_target[:,:,channel] @ kx + alpha*weights
        def hessian(w):
            return gain**2 * gy @ w @ gx + alpha*w
        def score(w):
            return float(np.sum(w*hessian(w))-2*np.sum(w*b))
        w = weights.copy()
        accelerated = w.copy()
        t = 1.
        for _ in range(300):
            new = np.clip(accelerated-(hessian(accelerated)-b)/lipschitz,0,1)
            next_t = (1+math.sqrt(1+4*t*t))/2
            accelerated = new+(t-1)/next_t*(new-w)
            w,t = new,next_t
        candidates = [weights.copy(), np.rint(w*steps)/steps]
        for candidate in candidates:
            gradient = hessian(candidate)-b
            for _ in range(12):
                changed = False
                for y,x in np.ndindex(candidate.shape):
                    value = np.clip(np.rint((candidate[y,x]-gradient[y,x]/diagonal[y,x])*steps[y,x])/steps[y,x],0,1)
                    delta = value-candidate[y,x]
                    if delta and 2*delta*gradient[y,x]+delta*delta*diagonal[y,x] < -1e-12:
                        candidate[y,x] = value
                        gradient += delta*gain**2*np.outer(gy[:,y],gx[x,:])
                        gradient[y,x] += alpha*delta
                        changed = True
                if not changed:
                    break
        best = min([weights]+candidates, key=score)
        for design in result:
            if design['channel'] != channel:
                continue
            for site in design['image_sites']:
                y,x = site['pixel_y'],site['pixel_x']
                drops = int(round(best[y,x]*steps[y,x]))
                fraction = drops/steps[y,x]
                set_site_fraction(design, site, fraction)
    optimized = render_gaussian_overlay(result,fwhm_nm,exposure,linear_light,normalize_designs,compensate_area)
    before = float(np.mean((baseline.density*signal_gain-target)**2))
    after = float(np.mean((optimized.density*signal_gain-target)**2))
    deviation = float(np.mean([(s['replacement_fraction']-base['replacement_fraction'])**2
        for d,original in zip(result,designs) for s,base in zip(d['image_sites'],original['image_sites'])]))
    penalty = regularization_strength * deviation
    if after + penalty > before:
        result,after = deepcopy(designs),before
        penalty = deviation = 0.0
    metrics = dict(method='bounded least squares with quantized coordinate refinement',
                   objective='mean squared unclipped signal error against fitted upright source + regularization * mean squared departure from direct assignments',
                   regularization_strength=regularization_strength, regularization_penalty=penalty,
                   mean_squared_assignment_change=deviation, baseline_regularized_objective=before,
                   optimized_regularized_objective=after+penalty,
                   linear_light=linear_light, normalize_designs=normalize_designs, compensate_area=compensate_area,
                   baseline_rmse=math.sqrt(before), optimized_rmse=math.sqrt(after),
                   mse_reduction_fraction=(before-after)/before if before else 0.,
                   fwhm_nm=fwhm_nm, exposure=exposure, barcode_bits_fixed=True)
    return result, metrics
