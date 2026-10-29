"""Residual rigid alignment errors, applied before decoded shifts.

The sampled mean and within-design covariance give expected squared image error
for N independent origami per design without treating sites as independent jitter.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import math
import numpy as np
from PIL import Image
from optimization_regularization import regularization_weight
from color_light import display_rgb, abundance_scale, canvas_area_gain, quadrant_weight
from gaussian_reconstruction import GaussianReconstruction, REFERENCE_AREA_NM2
from image_encoding import geometry, transfer_fraction_steps, set_site_fraction
from template_generator import EXTENSION_COLUMN_SPACING_NM, EXTENSION_ROW_SPACING_NM, EXTENSION_EXTRA_COLUMN_GAP_NM

DEFAULT_CENTER_X = -EXTENSION_COLUMN_SPACING_NM / 2 - EXTENSION_EXTRA_COLUMN_GAP_NM / 2
DEFAULT_CENTER_Y = 7 * EXTENSION_ROW_SPACING_NM / 2
TRAIN_SEED = 1729
VALIDATION_SEED = 2718
ERROR_SAMPLES = 32


@dataclass(frozen=True)
class AlignmentSettings:
    translation_x_nm: float = 3.0
    translation_y_nm: float = 3.0
    rotation_deg: float = 3.0
    origami_per_design: int = 20
    center_x_nm: float = DEFAULT_CENTER_X
    center_y_nm: float = DEFAULT_CENTER_Y
    variability_penalty: float = 0.0

    def validate(self):
        for name in ('translation_x_nm', 'translation_y_nm', 'rotation_deg', 'variability_penalty'):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError('Alignment errors and variability penalty must be finite and nonnegative.')
        if not all(math.isfinite(v) for v in (self.center_x_nm, self.center_y_nm)):
            raise ValueError('Alignment center coordinates must be finite.')
        if isinstance(self.origami_per_design, bool) or int(self.origami_per_design) != self.origami_per_design or not 1 <= self.origami_per_design <= 10000:
            raise ValueError('Origami count per design must be an integer from 1 to 10000.')


def transformed_sites(design, settings, error):
    """Error = [dx nm, dy nm, clockwise degrees] in aligned origami coordinates."""
    shift = np.array([design['x_shift_nm'], design['y_shift_nm']])
    points = np.array([[s['x_nm'], s['y_nm']] for s in design['image_sites']]) - shift
    center = np.array([settings.center_x_nm, settings.center_y_nm]) - np.asarray(design.get("active_canvas_offset_nm", [0, 0]))
    angle = math.radians(error[2])
    rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    return (points-center) @ rotation.T + center + np.asarray(error[:2]) + shift


class AlignmentModel:
    """Fixed sampled forward model shared by preview, optimization and evaluation."""
    def __init__(self, designs, settings, fwhm_nm=7.5, exposure=1.0, seed=TRAIN_SEED, linear_light=False, normalize_designs=False, compensate_area=False):
        settings.validate()
        if not designs or not math.isfinite(fwhm_nm) or fwhm_nm <= 0 or not math.isfinite(exposure) or exposure <= 0:
            raise ValueError('Valid designs, positive Gaussian width and exposure are required.')
        self.compensate_area = compensate_area
        self.linear_light, self.normalize_designs = linear_light, normalize_designs
        self.settings, self.fwhm, self.exposure, self.seed = settings, fwhm_nm, exposure, seed
        self.rotation = designs[0].get('origami_rotation_degrees_clockwise', 0)
        self.scheme = designs[0]['encoding_scheme']
        if any(d['encoding_scheme'] != self.scheme or d.get('origami_rotation_degrees_clockwise',0) != self.rotation for d in designs):
            raise ValueError('Alignment models require a single scheme and orientation.')
        self.info = geometry(self.scheme, self.rotation, designs[0].get('active_image_columns'))
        # Larger virtual mosaics need not use the single-origami 0.75 nm raster.
        # Still resolve FWHM with at least four samples and record the true pitch.
        area = self.info['field_width_nm'] * self.info['field_height_nm']
        step = min(max(.75, math.sqrt(area / 18000)), fwhm_nm / 4)
        self.width = math.ceil(self.info['field_width_nm']/step-1e-9)
        self.height = math.ceil(self.info['field_height_nm']/step-1e-9)
        if self.width*self.height > (80000 if self.info["tile_count"] > 4 else 20000):
            raise ValueError('Alignment raster is too large for this field. Increase Gaussian FWHM to reduce the required resolution.')
        self.dx = self.info['field_width_nm']/self.width
        self.dy = self.info['field_height_nm']/self.height
        self.xs = -self.info['pixel_pitch_x_nm']/2 + (np.arange(self.width)+.5)*self.dx
        self.ys = -self.info['pixel_pitch_y_nm']/2 + (np.arange(self.height)+.5)*self.dy
        self.sigma = fwhm_nm / math.sqrt(8*math.log(2))
        self.scale = abundance_scale(self.scheme, normalize_designs)
        self.area_gain = canvas_area_gain(self.scheme, compensate_area)
        self.gain = REFERENCE_AREA_NM2*exposure*self.scale*self.area_gain
        self.groups = [[d for d in designs if d['channel']==c] for c in range(3)]
        keys = [[(d['x_shift'],d['y_shift']) for d in group] for group in self.groups]
        if not keys[0] or keys[0] != keys[1] or keys[0] != keys[2]:
            raise ValueError('Alignment simulation requires matching color/shift designs.')
        self.signature = [[(d['name'], [(s['name'],s['x_nm'],s['y_nm']) for s in d['image_sites']]) for d in group] for group in self.groups]
        rng = np.random.default_rng(seed)
        errors = rng.normal(size=(ERROR_SAMPLES//2,3))
        errors = np.concatenate((errors,-errors))  # antithetic zero-mean quadrature
        errors *= [settings.translation_x_nm, settings.translation_y_nm, settings.rotation_deg]
        self.mean_blocks, self.covariances = [], []
        self.slices = []
        start = 0
        for design in self.groups[0]:
            count = len(design['image_sites'])
            average = np.zeros((self.width*self.height,count))
            second = np.zeros((count,count))
            for error in errors:
                kernel = self.kernel(transformed_sites(design,settings,error)) * quadrant_weight(design, normalize_designs)
                average += kernel/len(errors)
                second += (kernel.T@kernel)/len(errors)
            covariance = second-average.T@average
            self.mean_blocks.append(average)
            self.covariances.append((covariance+covariance.T)/2)
            self.slices.append(slice(start,start+count))
            start += count
        self.mean = np.concatenate(self.mean_blocks,axis=1)
        self.mean_blocks = []  # Release duplicate storage after concatenating.

    @property
    def size(self):
        return (self.height,self.width) if self.rotation in (90,270) else (self.width,self.height)

    def kernel(self, points):
        kx = np.exp(-.5*((self.xs[:,None]-points[None,:,0])/self.sigma)**2)
        ky = np.exp(-.5*((self.ys[:,None]-points[None,:,1])/self.sigma)**2)
        return (ky[:,None,:]*kx[None,:,:]).reshape(self.width*self.height,-1)/(2*math.pi*self.sigma**2)

    def weights(self, designs):
        groups = [[d for d in designs if d['channel']==c] for c in range(3)]
        signature = [[(d['name'], [(s['name'],s['x_nm'],s['y_nm']) for s in d['image_sites']]) for d in group] for group in groups]
        if signature != self.signature:
            raise ValueError('Design geometry differs from the alignment model.')
        weights = np.array([[s.get('effective_brightness', s['replacement_fraction']) for d in group for s in d['image_sites']] for group in groups]).T
        if not np.all(np.isfinite(weights)) or np.any(weights<0) or any(s["replacement_fraction"] < 0 or s["replacement_fraction"] > 1 for d in designs for s in d["image_sites"]):
            raise ValueError('Replacement fractions must be in [0,1]; effective brightness must be finite and nonnegative.')
        return weights

    def result(self, local, kind, **extra):
        density = np.rot90(local,-self.rotation//90).copy()
        linear = density*self.gain
        portrait = self.rotation in (90,270)
        metadata = dict(model='sampled_rigid_alignment_then_decoded_shift', preview_kind=kind,
            fwhm_nm=self.fwhm, exposure=self.exposure, alignment=asdict(self.settings),
            linear_light=self.linear_light, normalize_designs=self.normalize_designs,
            compensate_area=self.compensate_area, canvas_area_gain=self.area_gain,
            design_abundance_scale=self.scale, linear_rgb_gain=self.gain,
            display_encoding='sRGB' if self.linear_light else 'legacy direct RGB',
            alignment_error_frame='origami-aligned axes, before decoded shift and display rotation',
            alignment_center_frame='unshifted image-site coordinates; C6 H01 is (0,0)',
            alignment_distribution='independent zero-mean Gaussian dx, dy, angle; shared rigid transform within each origami',
            alignment_center_default='center of full C12–C1 / H01–H15 staple grid, including middle column gap',
            error_samples=ERROR_SAMPLES, model_seed=self.seed, encoding_scheme=self.scheme,
            origami_rotation_degrees_clockwise=self.rotation, coordinate_frame='upright image',
            raster_columns=density.shape[1], raster_rows=density.shape[0],
            raster_pixel_x_nm=self.dy if portrait else self.dx, raster_pixel_y_nm=self.dx if portrait else self.dy,
            field_width_nm=self.info['display_field_width_nm'], field_height_nm=self.info['display_field_height_nm'],
            first_sample_x_nm=-self.info['display_pixel_pitch_x_nm']/2+(self.dy if portrait else self.dx)/2,
            first_sample_y_nm=-self.info['display_pixel_pitch_y_nm']/2+(self.dx if portrait else self.dy)/2,
            reference_area_nm2=REFERENCE_AREA_NM2, barcode_sites_included=False,
            normalization='mean per design; normalization balances unequal quadrant sampling and fixes total design weight; shared exposure, no image-dependent scaling',
            finite_count_scope='alignment uncertainty only; fractional site intensities, no occupancy or photon noise',
            clipped_pixel_fraction=float(np.mean(np.any(linear>1+1e-6,axis=2))),
            edge_handling='Gaussian tails outside the field are cropped without renormalization', **extra)
        return GaussianReconstruction(density,display_rgb(linear,self.linear_light),metadata)

    def expected(self, designs):
        local = (self.mean@self.weights(designs)).reshape(self.height,self.width,3)
        return self.result(local,'sampled ensemble mean')

    def example(self, designs, seed=42):
        self.weights(designs)  # geometry and intensity validation
        rng = np.random.default_rng(seed)
        local = np.zeros((self.height,self.width,3))
        n = self.settings.origami_per_design
        for design in designs:
            weights = np.array([s.get('effective_brightness', s['replacement_fraction']) for s in design['image_sites']]) * quadrant_weight(design,self.normalize_designs)
            errors = rng.normal(size=(n,3))*[self.settings.translation_x_nm,self.settings.translation_y_nm,self.settings.rotation_deg]
            if not np.any(weights):
                continue
            for error in errors:
                points = transformed_sites(design,self.settings,error)
                kx = np.exp(-.5*((self.xs[:,None]-points[None,:,0])/self.sigma)**2)
                ky = np.exp(-.5*((self.ys[:,None]-points[None,:,1])/self.sigma)**2)
                local[:,:,design['channel']] += (ky*weights)@kx.T/(n*2*math.pi*self.sigma**2)
        return self.result(local,'finite-count alignment realization',example_seed=seed)

    def evaluate(self, designs, target):
        weights = self.weights(designs)
        local_target = np.rot90(np.asarray(target),self.rotation//90).reshape(-1,3)
        bias = float(np.mean((self.gain*self.mean@weights-local_target)**2))
        variance = sum(float(np.sum(weights[sl]*(cov@weights[sl]))) for sl,cov in zip(self.slices,self.covariances))
        variance = max(0.,variance*self.gain**2/(self.settings.origami_per_design*local_target.size))
        return dict(mean_image_mse=bias, alignment_variance=variance,
                    expected_mse=bias+variance,
                    objective_mse=bias+(1+self.settings.variability_penalty)*variance)

    def optimize(self, designs, target, regularization_strength=0.0):
        target = np.asarray(target,dtype=float)
        if target.shape != (self.size[1],self.size[0],3) or not np.all(np.isfinite(target)) or np.any((target<0)|(target>1)):
            raise ValueError('Target must be finite RGB in [0,1] on the alignment raster.')
        weights = self.weights(designs)
        result = deepcopy(designs)
        alpha = regularization_weight(regularization_strength, self.width*self.height, weights.shape[0])
        # Expected squared error = error of mean + variance / number of origami.
        factor = self.gain**2*(1+self.settings.variability_penalty)/self.settings.origami_per_design
        def apply_hessian(v):
            product = self.gain**2*(self.mean.T@(self.mean@v)) + alpha*v
            for sl,cov in zip(self.slices,self.covariances):
                product[sl] += factor*(cov@v[sl])
            return product
        # Nine-bit grids have thousands of variables: avoid a quadratic-size Hessian.
        hessian = None
        if weights.shape[0] <= 1500:
            hessian = self.gain**2*(self.mean.T@self.mean)
            hessian.flat[::hessian.shape[0]+1] += alpha
            for sl,cov in zip(self.slices,self.covariances):
                hessian[sl,sl] += factor*cov
            lipschitz = float(np.max(np.sum(np.abs(hessian),axis=1)))
            diagonal = np.diag(hessian)
        else:
            rows = self.gain**2*(self.mean.T@(self.mean@np.ones(weights.shape[0]))) + alpha
            diagonal = self.gain**2*np.einsum('ij,ij->j',self.mean,self.mean) + alpha
            for sl,cov in zip(self.slices,self.covariances):
                rows[sl] += factor*np.sum(np.abs(cov),axis=1)
                diagonal[sl] += factor*np.diag(cov)
            lipschitz = float(rows.max())
        local_target = np.rot90(target,self.rotation//90).reshape(-1,3)
        b = self.gain*self.mean.T@local_target + alpha*weights
        # The cached model describes geometry, which can be reused after a
        # volume or stock change. Quantization must use the current designs.
        current_groups = [[d for d in designs if d['channel'] == c] for c in range(3)]
        steps = np.array([[transfer_fraction_steps(d, s) for d in group for s in d['image_sites']] for group in current_groups]).T
        if np.any(steps<1) or np.any(steps!=np.rint(steps)):
            raise ValueError('Alignment optimization requires 25 nL transfer increments.')
        w = weights.copy()
        accelerated,t = w.copy(),1.
        for _ in range(250 if hessian is not None else 100):
            gradient = hessian@accelerated if hessian is not None else apply_hessian(accelerated)
            new = np.clip(accelerated-(gradient-b)/lipschitz,0,1)
            next_t = (1+math.sqrt(1+4*t*t))/2
            accelerated = new+(t-1)/next_t*(new-w)
            w,t = new,next_t
        best = weights.copy()
        for channel in range(3):
            def score(v):
                return float(v@(hessian@v if hessian is not None else apply_hessian(v))-2*v@b[:,channel])
            candidates = [weights[:,channel].copy(),np.rint(w[:,channel]*steps[:,channel])/steps[:,channel]]
            for candidate in candidates:
                if hessian is None:
                    residual = self.gain*(self.mean@candidate)-local_target[:,channel]
                    var_gradient = np.zeros_like(candidate)
                    groups = {}
                    for sl,cov in zip(self.slices,self.covariances):
                        var_gradient[sl] = factor*(cov@candidate[sl])
                        for i in range(sl.start,sl.stop):
                            groups[i] = (sl,cov,i-sl.start)
                else:
                    gradient = hessian@candidate-b[:,channel]
                for _ in range(10 if hessian is not None else 4):
                    changed = False
                    for i in range(len(candidate)):
                        if diagonal[i] < 1e-20:
                            continue
                        derivative = gradient[i] if hessian is not None else self.gain*(self.mean[:,i]@residual)+var_gradient[i]+alpha*(candidate[i]-weights[i,channel])
                        value = np.clip(np.rint((candidate[i]-derivative/diagonal[i])*steps[i,channel])/steps[i,channel],0,1)
                        delta = value-candidate[i]
                        if delta and 2*delta*derivative+delta*delta*diagonal[i] < -1e-12:
                            candidate[i] = value
                            if hessian is not None:
                                gradient += delta*hessian[:,i]
                            else:
                                residual += delta*self.gain*self.mean[:,i]
                                sl,cov,j = groups[i]
                                var_gradient[sl] += delta*factor*cov[:,j]
                            changed = True
                    if not changed:
                        break
            best[:,channel] = min([weights[:,channel]]+candidates,key=score)
        for channel in range(3):
            i = 0
            for design in result:
                if design['channel'] != channel:
                    continue
                for site in design['image_sites']:
                    drops = int(round(best[i,channel]*steps[i,channel]))
                    fraction = float(drops/steps[i,channel])
                    set_site_fraction(design, site, fraction)
                    i += 1
        before,after = self.evaluate(designs,target),self.evaluate(result,target)
        deviation = float(np.mean((self.weights(result)-weights)**2))
        penalty = regularization_strength * deviation
        if after['objective_mse'] + penalty > before['objective_mse']:
            result,after = deepcopy(designs),before
            penalty = deviation = 0.0
        metrics = dict(method='bounded expected-error optimization with within-origami covariance and droplet refinement',
            objective='mean image squared error + (1 + variability penalty) * finite-count alignment variance + regularization * mean squared departure from direct assignments',
            regularization_strength=regularization_strength, regularization_penalty=penalty,
            mean_squared_assignment_change=deviation, baseline_regularized_objective=before['objective_mse'],
            optimized_regularized_objective=after['objective_mse']+penalty,
            baseline_rmse=math.sqrt(before['expected_mse']),optimized_rmse=math.sqrt(after['expected_mse']),
            mse_reduction_fraction=(before['expected_mse']-after['expected_mse'])/before['expected_mse'] if before['expected_mse'] else 0.,
            training_baseline=before,training_optimized=after,alignment=asdict(self.settings),
            training_seed=self.seed,error_samples=ERROR_SAMPLES,barcode_bits_fixed=True,
            fwhm_nm=self.fwhm,exposure=self.exposure,linear_light=self.linear_light,normalize_designs=self.normalize_designs,compensate_area=self.compensate_area)
        return result,metrics
