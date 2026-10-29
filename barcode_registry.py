"""Persistent reservations for origami codes within a decoding experiment."""
from image_encoding import BARCODE_SCHEMA_KEYS
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from image_encoding import default_barcode_schema, validate_barcode_schema


def now():
    return datetime.now(timezone.utc).isoformat()


def location(name):
    match = re.fullmatch(r'(?:U-Apt|U-PAINT)_(H\d{2}-\d{2})(?:_R1)?', name)
    return match[1] if match else None


def layout(schema, count):
    schema = validate_barcode_schema(schema, count)
    return [group['sites'] for group in schema[BARCODE_SCHEMA_KEYS[count]]]


def decode(schema, count, fractions):
    groups = layout(schema, count)
    for site in schema.get('always_on_sites', []):
        if not math.isclose(fractions.get(site, 0), 1, abs_tol=1e-9):
            raise ValueError('Missing or partial fiducial: ' + site)
    bits = []
    for index, sites in enumerate(groups, 1):
        values = [fractions.get(site, 0) for site in sites]
        if all(math.isclose(v, 0, abs_tol=1e-9) for v in values):
            bits.append(0)
        elif all(math.isclose(v, 1, abs_tol=1e-9) for v in values):
            bits.append(1)
        else:
            raise ValueError('Invalid bit {}: all grouped sites must be fully ON or fully OFF ({}).'.format(index, ', '.join(sites)))
    return ''.join(map(str, bits))


def record(schema, count, fractions, experiment, run_id, run_name='', plate='', wells=(), origin='manual'):
    experiment = experiment.strip()
    if not experiment:
        raise ValueError('Enter an experiment/pool name for barcode checking.')
    schema = validate_barcode_schema(schema, count)
    bits = decode(schema, count, fractions)
    groups = layout(schema, count)
    # Compare physical site groups, independent of bit labels/order or schema name.
    layout_key = sorted(sorted(group) for group in groups)
    on_sites = sorted(site for bit, group in zip(bits, groups) if bit == '1' for site in group)
    return dict(id=run_id, experiment=experiment, run_name=run_name, origin=origin,
                destination_plate_name=plate, destination_wells=list(wells), bits=bits,
                schema=schema, bit_count=count, fractions=dict(fractions), layout_key=layout_key,
                on_sites=on_sites, created_utc=now(), released=False)


def from_rgb(run, experiment):
    design = run['image_design']
    count = len(design['barcode_sites'])
    schema = default_barcode_schema()
    schema['name'] = 'RGB design barcode schema'
    schema[BARCODE_SCHEMA_KEYS[count]] = [
        dict(bit=i+1, sites=['-'.join(site) for site in group]) for i, group in enumerate(design['barcode_sites'])]
    schema['always_on_sites'] = ['-'.join(site) for site in design.get('always_on_sites', [])]
    fractions = {location(name): value for name, value in design['site_fractions'].items() if location(name)}
    identity = run.get('source_run_id') or run.get('id')
    if not identity:
        identity = hashlib.sha256(json.dumps([design, run.get('destination_plate_name'), run.get('destination_wells')], sort_keys=True).encode()).hexdigest()
    return record(schema, count, fractions, experiment, identity, run.get('run_name', design['name']),
                  run.get('destination_plate_name', ''), run.get('destination_wells', []), 'RGB')


class BarcodeRegistry:
    def __init__(self, path):
        self.path = Path(path)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'format':'origami-barcode-registry-v1','entries':[],'events':[]}
        if self.state.get('format') != 'origami-barcode-registry-v1':
            raise ValueError('Unsupported barcode registry format.')

    @property
    def entries(self):
        return self.state['entries']

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(json.dumps(self.state, indent=2) + '\n')
        temporary.replace(self.path)

    def check(self, candidate, additional=()):
        for entry in [*self.entries, *additional]:
            if entry.get('released') or entry['experiment'] != candidate['experiment'] or entry['id'] == candidate['id']:
                continue
            description = '{} · {} {}'.format(entry['run_name'], entry['destination_plate_name'], ', '.join(entry['destination_wells']))
            if entry['layout_key'] != candidate['layout_key']:
                raise ValueError('Incompatible barcode layouts in experiment {!r}: {}. For a separately decoded batch, enter a new Experiment / pool name in RGB run settings. Samples pooled together must use the same decoding schema. Starting a fresh destination plate does not reset barcode reservations.'.format(candidate['experiment'], description))
            if entry['on_sites'] == candidate['on_sites']:
                raise ValueError('Already used: code {} conflicts with {}.'.format(candidate['bits'], description))

    def reserve(self, records, check=True):
        records = list(records)
        if check:
            for index, entry in enumerate(records):
                self.check(entry, records[:index])
        known = {(entry['experiment'], entry['id']):entry for entry in self.entries}
        additions = []
        updated = False
        for entry in records:
            key = (entry['experiment'], entry['id'])
            if key in known:
                existing = known[key]
                if not existing.get('released') and existing['on_sites'] == entry['on_sites'] and existing['layout_key'] == entry['layout_key']:
                    for field in ('run_name','destination_plate_name','destination_wells'):
                        if existing[field] != entry[field]:
                            existing[field] = deepcopy(entry[field]); updated = True
                continue
            if not check and entry['destination_plate_name'] and entry['destination_wells'] and any(
                old['experiment'] == entry['experiment'] and old['run_name'] == entry['run_name']
                and old['destination_plate_name'] == entry['destination_plate_name']
                and old['destination_wells'] == entry['destination_wells']
                and old['layout_key'] == entry['layout_key'] and old['on_sites'] == entry['on_sites']
                for old in [*self.entries,*additions]):
                continue # Older manifests lack run IDs; identify the same physical run.
            additions.append(deepcopy(entry)); known[key] = additions[-1]
        if additions:
            self.entries.extend(additions)
            self.state['events'].append(dict(action='reserve' if check else 'import', time=now(), ids=[r['id'] for r in additions]))
        if additions or updated:
            self.save()
        return len(additions)

    def release(self, entry):
        entry['released'] = True
        self.state['events'].append(dict(action='release', time=now(), id=entry['id'], experiment=entry['experiment']))
        self.save()

    def import_payload(self, payload, experiment):
        records = []
        if not isinstance(payload, dict):
            raise ValueError('Barcode import must be a JSON object.')
        if payload.get('format') == 'origami-barcode-registry-v1':
            for entry in payload['entries']:
                if not entry.get('released'):
                    records.append(record(entry['schema'], entry['bit_count'], entry['fractions'], experiment,
                        entry['id'], entry['run_name'], entry['destination_plate_name'], entry['destination_wells'], entry.get('origin','import')))
        elif payload.get('format') == 'origami-rgb-image-v2':
            runs = {run['run_name']: run for run in payload.get('runs', [])}
            for design in payload['designs']:
                run = dict(runs.get(design['name'], {}), image_design=design)
                records.append(from_rgb(run, experiment))
        else:
            raise ValueError('Choose image_manifest.json or an exported barcode registry JSON.')
        if not records:
            raise ValueError('This file contains no active barcode records.')
        return self.reserve(records, check=False)
