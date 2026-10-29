"""Build the ordered 20 µM base plate from four IDT 96-well sheets."""
import argparse
import csv
import re
from pathlib import Path


def add_biotin_inventory(records):
    """Retain the ordered staples and inventory the eight later additions."""
    path = Path(__file__).resolve().parents[1] / 'replacement_sheets' / 'lowconc_D_biotin_additions.csv'
    with path.open(newline='') as handle:
        next(handle)
        additions = list(csv.DictReader(handle))
    occupied = {row['Well Position'] for row in records}
    result = [dict(row, Usage='') for row in records]
    for row in additions:
        if row['Well'] in occupied:
            raise ValueError(f"D-Biotin addition would overwrite occupied well {row['Well']}")
        occupied.add(row['Well'])
        result.append({'Well Position': row['Well'], 'Sequence Name': row['Name'], 'Sequence': row['Sequence'],
                       'Original Well Position': row['Replace Well'], 'Hinge Type': '',
                       'Nominal Concentration µM': row['Nominal Concentration µM'],
                       'IDT Source Plate': '', 'IDT Source Well': '', 'Previous Quadrant Well': '', 'Usage': 'Supplemental'})
    return result


def build(workbook, reference, output):
    import openpyxl
    with Path(reference).open(encoding='utf-8-sig', newline='') as handle:
        next(handle)
        known = {r['Sequence Name']: r for r in csv.DictReader(handle)}
    book = openpyxl.load_workbook(workbook, data_only=True)
    result = []
    for plate, sheet in enumerate(list(book)[:4], 1):
        for well, name, sequence, _, old_well in sheet.iter_rows(min_row=2, max_col=5, values_only=True):
            if not well and not name:
                continue
            match = re.fullmatch(r'([A-H])(0?[1-9]|1[0-2])', str(well))
            if not match or not name or not sequence:
                raise ValueError(f'Incomplete source record: {sheet.title}: {well}')
            row, col = ord(match[1])-ord('A'), int(match[2])
            quadrant_well = f'{chr(65 + row + (8 if plate > 2 else 0))}{col + (12 if plate in (2,4) else 0):02d}'
            if old_well != quadrant_well:
                raise ValueError(f'Unexpected original location: {name}: {old_well}')
            prior = known.get(name)
            if prior:
                if ''.join(sequence.split()) != ''.join(prior['Sequence'].split()):
                    raise ValueError(f'Sequence mismatch: {name}')
                logical = prior['Original Well Position'] or prior['Well Position']
                hinge = prior['Hinge Type']
            else:
                # Updated ordered staples explicitly identify their logical site.
                site = re.fullmatch(r'([A-P]\d{2})_ST.+', name)
                if not site:
                    raise ValueError(f'Unknown logical replacement site: {name}')
                logical, hinge = site[1], ''
                if logical not in {r['Original Well Position'] or r['Well Position'] for r in known.values()}:
                    raise ValueError(f'Unknown replacement key: {logical}')
            physical = f'{chr(65 + 2*row + (1 if plate > 2 else 0))}{col + (12 if plate in (2,4) else 0):02d}'
            result.append(dict(zip(
                ['Well Position','Sequence Name','Sequence','Original Well Position','Hinge Type','Nominal Concentration µM','IDT Source Plate','IDT Source Well','Previous Quadrant Well'],
                [physical,name,sequence,logical,hinge,20,plate,well,old_well])))
    for key in ('Well Position','Sequence Name','Original Well Position'):
        if len({r[key] for r in result}) != len(result):
            raise ValueError(f'Duplicate {key}')
    result = add_biotin_inventory(result)
    result.sort(key=lambda r: r['Well Position'])
    with Path(output).open('w', encoding='utf-8', newline='') as handle:
        handle.write('Plate SourcePlate8[8]\n')
        writer = csv.DictWriter(handle, fieldnames=list(result[0]))
        writer.writeheader()
        writer.writerows(result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook', type=Path)
    parser.add_argument('reference', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(f'Wrote {len(build(args.workbook, args.reference, args.output))} staples to {args.output}')
