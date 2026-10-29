"""Preview R1/body competition without changing synthesis assignments.

Image-site names identify source staples in the RGB tab's shared R1 source map.
A stock error is shared by every design using that staple, across RGB channels.
"""
from copy import deepcopy
import math
import numpy as np


def brightness_cases(designs, ratio=1.10, cv_percent=10, seed=42, transfer_cv_percent=0):
    if not all(math.isfinite(v) for v in (ratio, cv_percent, transfer_cv_percent)) or ratio <= 0 or cv_percent < 0 or transfer_cv_percent < 0:
        raise ValueError('R1/body ratio must be finite and positive; source and transfer CVs must be finite and nonnegative.')
    if not isinstance(seed, int) or seed < 0:
        raise ValueError('Seed must be a nonnegative integer.')
    names=set()
    for design in designs:
        for site in design['image_sites']:
            p=site['replacement_fraction']
            if not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError('Intended R1 fractions must be in [0,1].')
            if not site.get('name'):
                raise ValueError('Image sites need source staple names for shared stock errors.')
            names.add(site['name'])
    # Positive multiplicative error with arithmetic mean 1 and requested CV.
    # Work in log space to avoid overflowing ratios at large CVs.
    sigma=math.sqrt(2*math.log(math.hypot(1, cv_percent/100)))
    rng=np.random.default_rng(seed)
    log_errors=dict(zip(sorted(names), rng.normal(-sigma*sigma/2, sigma, len(names))))
    # Independent positive volume multipliers for R1 and competitor transfers.
    # A transfer means the total volume delivered for a source/site/design,
    # not each acoustic droplet. Keep this random stream separate from stocks.
    transfer_sigma=math.sqrt(2*math.log(math.hypot(1, transfer_cv_percent/100)))
    transfer_rng=np.random.default_rng(np.random.SeedSequence([seed, 1]))
    def design_key(index, design):
        return (str(design.get('name', index)), str(design.get('channel', '')))
    keys=sorted({(*design_key(i,d),s['name']) for i,d in enumerate(designs) for s in d['image_sites']})
    volume_logs=transfer_rng.normal(-transfer_sigma*transfer_sigma/2,transfer_sigma,(len(keys),2))
    transfer_errors={key:float(pair[0]-pair[1]) for key,pair in zip(keys,volume_logs)}
    cases=[]
    for systematic, random in [(False,False),(True,False),(False,True),(True,True)]:
        result=deepcopy(designs)
        for index, design in enumerate(result):
            for site in design['image_sites']:
                p=site['replacement_fraction']
                log_ratio=(math.log(ratio) if systematic else 0)+(log_errors[site['name']] + transfer_errors[(*design_key(index, design), site['name'])] if random else 0)
                if p in (0,1) or log_ratio == 0:
                    value=p
                else:
                    # g*p/(g*p+1-p), evaluated as a stable logistic function.
                    odds=math.log(p)-math.log1p(-p)+log_ratio
                    value=1/(1+math.exp(-odds)) if odds>=0 else math.exp(odds)/(1+math.exp(odds))
                site['effective_brightness']=float(value)
        cases.append(result)
    return cases
