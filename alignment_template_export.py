"""Export PAINT Analysis templates from a prepared RGB batch snapshot."""
from image_encoding import BARCODE_SCHEMA_KEYS
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile

from barcode_template_schema import to_template
from template_generator import logical_template_sites, save_barcode_template


def export_alignment_templates(destination, snapshot, runs):
    """Write a complete bundle atomically, without changing wells or designs."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Alignment-template destination already exists: {}'.format(destination))
    designs = snapshot['designs']
    if not designs:
        raise ValueError('Prepare at least one RGB design before exporting templates.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.'+destination.name+'-', dir=destination.parent))
    try:
        templates = staging/'barcodes'
        templates.mkdir()
        run_lookup = {run['run_name']: run for run in runs}
        models = {}
        entries = []
        for index, design in enumerate(designs, 1):
            count = len(design['bits'])
            if count not in models:
                models[count] = to_template(snapshot['barcode_schema'], count)
            model = models[count]
            expected = [[list(site.split('-')) for site in group['sites']]
                        for group in snapshot['barcode_schema'][BARCODE_SCHEMA_KEYS[count]]]
            if design.get('barcode_sites', expected) != expected:
                raise ValueError('Prepared barcode sites differ from the schema snapshot. Prepare runs again.')
            active = ['bit_{}'.format(i+1) for i, bit in enumerate(design['bits']) if bit]
            sites = logical_template_sites(8,12,active,schema=model)
            run = run_lookup[design['name']]
            entry = dict(design_name=design['name'], color=design['color'], barcode_code=design['barcode_code'],
                         bits=design['bits'], encoding_scheme=design['encoding_scheme'],
                         x_shift_nm=design['x_shift_nm'], y_shift_nm=design['y_shift_nm'],
                         origami_rotation_degrees_clockwise=design.get('origami_rotation_degrees_clockwise',0),
                         destination_plate_name=run['destination_plate_name'], destination_wells=run['destination_wells'])
            if sites:
                # Numeric prefixes keep filenames unique and independent of labels.
                filename='design_{:04d}_code_{:03d}.png'.format(index,design['barcode_code'])
                save_barcode_template(templates/filename,8,12,sites,
                                      active_logical_bits=active,logical_schema=deepcopy(model))
                entry.update(png='barcodes/'+filename, metadata='barcodes/'+Path(filename).with_suffix('.json').name)
            else:
                entry.update(png=None,metadata=None,reason='All barcode bits are off and no fiducials are defined; no bright alignment sites.')
            entries.append(entry)
        fiducial_entries=[]
        for count, model in models.items():
            sites=logical_template_sites(8,12,[],schema=model)
            if sites:
                folder=staging/'fiducials'; folder.mkdir(exist_ok=True)
                filename='fiducials_{}bit.png'.format(count)
                save_barcode_template(folder/filename,8,12,sites,active_logical_bits=[],logical_schema=deepcopy(model))
                fiducial_entries.append('fiducials/'+filename)
        manifest=dict(format='paint-analysis-rgb-alignment-templates-v1',batch_id=snapshot['batch_id'],
                      template_coordinate_frame='Canonical origami lattice, mirrored C12 to C1 as recorded in each PNG; not the composite image rotation.',
                      barcode_schema=snapshot['barcode_schema'], geometry=snapshot['geometry'],
                      templates=entries,fiducial_templates=fiducial_entries,
                      exported_count=sum(e['png'] is not None for e in entries),
                      skipped_count=sum(e['png'] is None for e in entries))
        (staging/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        (staging/'barcode_schema.json').write_text(json.dumps(snapshot['barcode_schema'],indent=2)+'\n')
        (staging/'README.txt').write_text(
            'PAINT Analysis alignment templates\n\n'
            'Load the PNGs in barcodes/ as custom barcode templates for classification.\n'
            'Each contains the ON barcode groups and permanent fiducials for its design.\n'
            'fiducials/ contains shared fiducial-only references, when defined.\n'
            'PNG files embed nm-per-pixel calibration and the complete logical-bit model;\n'
            'the same metadata is provided in adjacent JSON files.\n'
            'Templates use the full origami lattice; analog image pixels are excluded.\n'
            'manifest.json maps filenames to design names, codes, colors, shifts and wells.\n'
            'All-off designs without fiducials have no bright sites and are listed as skipped.\n')
        staging.rename(destination)
        return manifest
    except Exception:
        shutil.rmtree(staging,ignore_errors=True)
        raise
