"""Rank all layouts by source-content RGB error on a shared scoring raster."""
from pathlib import Path
from copy import deepcopy
from dataclasses import replace
import csv
import json
import os
import shutil
import tempfile
import numpy as np
from PIL import Image

from alignment_reconstruction import AlignmentModel, VALIDATION_SEED
from gaussian_reconstruction import render_gaussian_overlay
from gaussian_optimization import fitted_target
from image_encoding import FIT_MODES, SELECTABLE_SCHEMES
from exhaustive_pdf import display_region, generate_views, report_jobs, ExportCancelled, settings_lines


def source_content_rmse(result, target, region):
    """Compare unclipped signal, excluding only known fitting margins."""
    signal = result.density * result.metadata['linear_rgb_gain']
    bounds = (region[0]*signal.shape[1], region[1]*signal.shape[0],
              region[2]*signal.shape[1], region[3]*signal.shape[0])
    resized = np.stack([np.asarray(Image.fromarray(signal[:,:,c].astype(np.float32)).resize(
        (target.shape[1], target.shape[0]), Image.Resampling.BOX, box=bounds)) for c in range(3)], axis=2)
    return float(np.sqrt(np.mean((resized-target)**2)))


def score_assignments(settings, info, baseline, optimized, validation=None):
    region, (width, height) = display_region(settings, info)
    scale = 256/max(width, height)
    size = (max(1,round(width*scale)), max(1,round(height*scale)))
    # When padding is removed, the target is the complete upright source.
    target_mode = 'Stretch' if settings.fit_mode == FIT_MODES[0] else settings.fit_mode
    target = fitted_target(settings.image_path, target_mode, size, linear_light=settings.linear_light)
    if settings.alignment_enabled:
        if validation is None:
            validation = AlignmentModel(baseline, settings.alignment, settings.fwhm_nm, settings.exposure,
                                        seed=VALIDATION_SEED, **settings.light_options())
        renderer = validation.expected
    else:
        renderer = lambda ds: render_gaussian_overlay(ds, settings.fwhm_nm, settings.exposure, **settings.light_options())
    before = source_content_rmse(renderer(baseline), target, region)
    after = source_content_rmse(renderer(optimized), target, region)
    key = info['encoding_scheme']
    return dict(scheme_id=key, label=info['encoding_label'], rotation=info['origami_rotation_degrees_clockwise'],
                design_count=info['design_count'], tile_count=info['tile_count'],
                baseline_rmse=before, final_rmse=after,
                evaluation='held-out expected alignment image' if settings.alignment_enabled else 'ideal alignment image',
                evaluation_seed=VALIDATION_SEED if settings.alignment_enabled else '',
                linear_light=settings.linear_light, scoring_width=size[0], scoring_height=size[1])


def draw_ranking(rows, settings, pdf_path, png_path):
    # Use a noninteractive backend: chart export runs outside the Tk thread.
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.lines import Line2D
    ranked = sorted(rows, key=lambda row: (row['final_rmse'], row['design_count'], row['scheme_id']))
    fig = Figure(figsize=(14, max(6, .31*len(ranked)+3.6)), dpi=150)
    FigureCanvasAgg(fig)
    ax = fig.add_axes((.40, .14, .54, .76))
    y = np.arange(len(ranked))
    before = [row['baseline_rmse'] for row in ranked]
    after = [row['final_rmse'] for row in ranked]
    colors = ['#177b83' if row['design_count'] <= 64 else '#b76425' for row in ranked]
    ax.hlines(y, before, after, color='#bbc4c6', linewidth=1.7)
    ax.scatter(before, y, facecolors='none', edgecolors='#777777', s=35, zorder=3)
    ax.scatter(after, y, c=colors, s=40, zorder=4)
    labels = []
    for rank, row in enumerate(ranked, 1):
        scheme = SELECTABLE_SCHEMES[row['scheme_id']]
        label = ('{}×{} tiles; {}×{} shifts'.format(scheme.tile_columns,scheme.tile_rows,scheme.x_counts[0],scheme.y_counts[0])
                 if row['scheme_id'].startswith('tiling_') else scheme.label.split(' — ')[0].split(' —')[0])
        if row['scheme_id'].startswith('sweep_'):
            label = scheme.label.replace(' budget', '').replace(' local shifts', ' shifts')
        labels.append('{}  {}  | {} types | {}°'.format(rank,label,row['design_count'],row['rotation']))
    ax.set_yticks(y, labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(left=0, right=max(before+after+[.01])*1.15)
    ax.set_xlabel('RGB RMSE — lower is closer to the uploaded image', fontsize=11)
    ax.grid(axis='x', color='#dddddd', linewidth=.7)
    ax.set_axisbelow(True)
    for spine in ('top','right','left'):
        ax.spines[spine].set_visible(False)
    ax.tick_params(axis='y', length=0)
    for i,row in enumerate(ranked):
        ax.annotate('{:.4f}'.format(row['final_rmse']), (row['final_rmse'],i), xytext=(6,5),
                    textcoords='offset points', fontsize=7, color=colors[i])
    ax.legend(handles=[Line2D([],[],marker='o',linestyle='',markerfacecolor='none',color='#777777',label='Before optimization'),
                       Line2D([],[],marker='o',linestyle='',color='#177b83',label='Final: ≤64 types'),
                       Line2D([],[],marker='o',linestyle='',color='#b76425',label='Final: >64 types')],
              loc='lower right', fontsize=8)
    fig.suptitle('Similarity to the uploaded image', fontsize=20, x=.06, ha='left', y=.97)
    fig.text(.06,.938, 'All layouts ranked by final RGB error · best-fitting 0°/90° orientation', fontsize=11)
    notes = [
        ('Independent alignment samples (seed {}). Expected image; finite-count variance is not included in this score.'.format(VALIDATION_SEED)
         if settings.alignment_enabled else 'Ideal-alignment prediction; alignment simulation is disabled.'),
        '{}; unclipped signal. Known fitting padding excluded; common source-content raster (256 px longest side).'.format(
            'Linear light' if settings.linear_light else 'Direct RGB'),
        'Image RMSE is a numerical measure, not a perceptual similarity percentage. Regularization penalty is excluded.',
        'FWHM {:g} nm · exposure {:g}× · regularization {:g} · optimization {} · brightness matching {}'.format(
            settings.fwhm_nm,settings.exposure,settings.regularization_strength,settings.optimize,settings.compensate_area),
        'Source fit: {}. Center crop compares cropped source regions; Fit with black padding compares the full source.'.format(settings.fit_mode),
        'Counts matter: nine-bit layouts require more origami types. Duplicate dropdown layouts are retained.'
    ]
    for i,note in enumerate(notes):
        fig.text(.06,.095-i*.014,note,fontsize=8)
    fig.savefig(pdf_path)
    fig.savefig(png_path, dpi=160)
    fig.clear()
    return ranked


def export_similarity(destination, settings, progress=lambda done,total,phase:None, cancelled=lambda:False, schemes=None):
    settings.validate()
    destination = Path(destination).with_suffix('.pdf')
    keys = tuple(SELECTABLE_SCHEMES if schemes is None else schemes)
    def check(done, phase):
        if cancelled():
            raise ExportCancelled()
        progress(done,len(keys),phase)
    check(0,'Preparing similarity comparison')
    with tempfile.TemporaryDirectory(prefix='.rgb-similarity-',dir=destination.parent) as folder:
        folder = Path(folder)
        source = folder/('source'+Path(settings.image_path).suffix)
        shutil.copyfile(settings.image_path,source)
        snapshot = replace(deepcopy(settings),image_path=str(source),multipliers=dict(settings.multipliers))
        rows = []
        for index,(key,rotation) in enumerate(report_jobs(source,keys,snapshot.active_image_columns)):
            rows.append(generate_views(snapshot,key,rotation,
                checkpoint=lambda phase,index=index,key=key:check(index,SELECTABLE_SCHEMES[key].label+' | '+phase), score_only=True))
            check(index+1,'Scored '+SELECTABLE_SCHEMES[key].label)
        check(len(keys),'Drawing ranked graph')
        pdf, png, csv_path, metadata = [folder/('ranking'+suffix) for suffix in ('.pdf','.png','.csv','.json')]
        ranked = draw_ranking(rows, settings, pdf, png)
        with csv_path.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['rank']+list(ranked[0]))
            writer.writeheader()
            for rank,row in enumerate(ranked,1):
                writer.writerow(dict(rank=rank,**row))
        metadata.write_text(json.dumps(dict(settings=settings_lines(settings), ranking=ranked),indent=2))
        check(len(keys),'Saving graph and scores')
        paths=[]
        for path in (pdf,png,csv_path,metadata):
            target=destination.with_suffix(path.suffix)
            os.replace(path,target)
            paths.append(str(target))
    return dict(paths=paths,ranking=ranked)
