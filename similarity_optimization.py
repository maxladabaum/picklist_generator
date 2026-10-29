"""Nominal optimization followed by a fixed-assignment factorial robustness sweep."""
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path
from copy import deepcopy
import csv
import json
import os
import shutil
import tempfile
import numpy as np

from alignment_reconstruction import AlignmentModel
from exhaustive_pdf import ExportCancelled, display_region, generate_views, report_jobs, settings_lines
from gaussian_optimization import fitted_target
from image_encoding import SELECTABLE_SCHEMES, FIT_MODES, geometry
from similarity_graph import source_content_rmse

FACTORS = (.5, 1., 1.5, 2.)
ROBUSTNESS_SEED = 31415  # Independent of training and nominal shortlisting.
PARAMETERS = ('translation_x_nm', 'translation_y_nm', 'rotation_deg', 'fwhm_nm')


def conditions(settings, factors=FACTORS):
    values = (settings.alignment.translation_x_nm, settings.alignment.translation_y_nm,
              settings.alignment.rotation_deg, settings.fwhm_nm)
    # Multiplying zero yields duplicate conditions. Evaluate those only once.
    axes = [tuple(dict.fromkeys(value*factor for factor in factors)) for value in values]
    return [dict(zip(PARAMETERS, values)) for values in product(*axes)]


def evaluate_condition(settings, designs, condition, target, region):
    alignment = replace(settings.alignment, **{k:condition[k] for k in PARAMETERS[:3]})
    model = AlignmentModel(designs, alignment, condition['fwhm_nm'], settings.exposure,
                           seed=ROBUSTNESS_SEED, **settings.light_options())
    return source_content_rmse(model.expected(designs), target, region)


def rank_robustness(rows):
    return sorted(rows, key=lambda row:(row['worst_rmse'], row['mean_rmse'],
                                       row['final_rmse'], row['scheme_id']))


def write_csv(path, rows):
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def draw_report(path, png, rows, scenarios, settings):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.backends.backend_pdf import PdfPages
    with PdfPages(path) as pdf:
        fig = Figure(figsize=(15, max(7, .34*len(rows)+3)))
        FigureCanvasAgg(fig)
        ax = fig.add_axes((.46,.23,.50,.65))
        y = np.arange(len(rows))
        for field, marker, label in (('final_rmse','o','Nominal (shortlisting seed)'),
                                     ('mean_rmse','s','Mean across sweep'),
                                     ('worst_rmse','D','Worst case — determines winner')):
            ax.scatter([r[field] for r in rows],y,marker=marker,label=label,s=25)
        ax.set_yticks(y,['{}  {} | {} types | {}°'.format(i+1,r['label'].replace(' budget',''),
                         r['design_count'],r['rotation']) for i,r in enumerate(rows)],fontsize=7)
        ax.invert_yaxis()
        ax.set_xlabel('Source-image RGB RMSE — lower is better')
        ax.set_xlim(left=0)
        ax.grid(axis='x',alpha=.25)
        ax.legend(fontsize=8)
        fig.suptitle('Similarity optimization: robustness finalists',fontsize=18,y=.97)
        notes = [
            'Winner: '+rows[0]['label'],
            'Ranked by lowest worst-case RMSE; ties use mean RMSE. Assignments and orientation remain fixed.',
            'Each axis: 50%, 100%, 150%, 200% of its starting value; full Cartesian product. Zero errors remain zero.',
            'Nominal optimization and alignment simulation are enabled for this analysis; other RGB settings are retained.',
            'Independent stress-test seed {}. Expected image only; finite-count variance is excluded from ranking.'.format(ROBUSTNESS_SEED),
            'Starting X/Y SD {:g}/{:g} nm; rotation SD {:g}°; FWHM {:g} nm. Source fit: {}.'.format(
                settings.alignment.translation_x_nm,settings.alignment.translation_y_nm,
                settings.alignment.rotation_deg,settings.fwhm_nm,settings.fit_mode),
            'Full settings, nominal scores, every stress score, and winning assignments are saved alongside this report.'
        ]
        for i,note in enumerate(notes):
            fig.text(.04,.17-i*.021,note,fontsize=9)
        pdf.savefig(fig)
        fig.savefig(png,dpi=140)
        fig.clear()
        fig = Figure(figsize=(13,10)); FigureCanvasAgg(fig)
        for index,parameter in enumerate(PARAMETERS,1):
            ax = fig.add_subplot(2,2,index)
            for rank,row in enumerate(rows,1):
                samples = [s for s in scenarios if s['scheme_id']==row['scheme_id']]
                xs = sorted({s[parameter] for s in samples})
                ys = [max(s['rmse'] for s in samples if s[parameter]==x) for x in xs]
                ax.plot(xs,ys,marker='.',linewidth=2 if rank==1 else .8,label=str(rank))
            ax.set_xlabel(parameter.replace('_',' '))
            ax.set_ylabel('Worst RMSE across other three axes')
            ax.grid(alpha=.2)
            if index==1:
                ax.legend(title='Final rank',ncol=4,fontsize=7)
        fig.suptitle('Sensitivity of frozen assignments — ranks match page 1')
        fig.tight_layout(rect=(0,0,1,.95)); pdf.savefig(fig); fig.clear()
        fig = Figure(figsize=(12,9)); FigureCanvasAgg(fig)
        import textwrap
        text = '\n'.join('\n'.join(textwrap.wrap(line,110)) for line in settings_lines(settings))
        fig.text(.05,.94,'Starting settings',fontsize=18)
        fig.text(.05,.88,text,va='top',fontsize=10,linespacing=1.7)
        fig.text(.05,.15,'This winner is conditional on this source, scoring metric, shortlist, sampled grid, and alignment model.\n'
                 'Unclipped signal is scored on a shared source-content raster. Regularization penalty is not part of the score.\n'
                 'Equivalent dropdown entries remain separate candidates. The active RGB design is not changed.',fontsize=10)
        pdf.savefig(fig); fig.clear()


def export_optimization(destination, settings, progress=lambda done,total,phase:None,
                        cancelled=lambda:False, schemes=None, top_n=20, factors=FACTORS):
    settings.validate()
    if top_n < 1 or not factors or any(not np.isfinite(f) or f <= 0 for f in factors):
        raise ValueError('Choose a positive finalist count and finite positive sweep factors.')
    settings = replace(settings,optimize=True,alignment_enabled=True)
    keys = tuple(SELECTABLE_SCHEMES if schemes is None else schemes)
    if not keys:
        raise ValueError('No schemes to compare.')
    grid = conditions(settings,factors)
    total = len(keys)+min(top_n,len(keys))*len(grid)
    def check(done,phase):
        if cancelled():
            raise ExportCancelled()
        progress(done,total,phase)
    destination = Path(destination).with_suffix('.pdf')
    check(0,'Stage 1: optimizing and ranking all schemes')
    with tempfile.TemporaryDirectory(prefix='.rgb-robustness-',dir=destination.parent) as folder:
        folder = Path(folder)
        source = folder/('source'+Path(settings.image_path).suffix)
        shutil.copyfile(settings.image_path,source)
        snapshot = replace(deepcopy(settings),image_path=str(source),multipliers=dict(settings.multipliers))
        nominal, finalists = [], []
        for i,(key,rotation) in enumerate(report_jobs(source,keys,snapshot.active_image_columns)):
            row,designs = generate_views(snapshot,key,rotation,
                checkpoint=lambda phase,i=i,key=key:check(i,'Stage 1 | '+SELECTABLE_SCHEMES[key].label+' | '+phase),
                score_only='assignments')
            nominal.append(row)
            finalists.append((row,designs))
            finalists.sort(key=lambda pair:(pair[0]['final_rmse'],pair[0]['design_count'],pair[0]['scheme_id']))
            del finalists[top_n:]
            check(i+1,'Stage 1: {}/{} schemes complete'.format(i+1,len(keys)))
        nominal.sort(key=lambda r:(r['final_rmse'],r['design_count'],r['scheme_id']))
        scenarios, summaries = [], []
        done = len(keys)
        for rank,(row,designs) in enumerate(finalists,1):
            info = geometry(row['scheme_id'],row['rotation'],snapshot.active_image_columns)
            region,(width,height) = display_region(snapshot,info)
            size = (row['scoring_width'],row['scoring_height'])
            mode = 'Stretch' if snapshot.fit_mode==FIT_MODES[0] else snapshot.fit_mode
            target = fitted_target(source,mode,size,linear_light=snapshot.linear_light)
            values=[]
            for j,condition in enumerate(grid):
                check(done,'Stage 2 | finalist {}/{} | condition {}/{} | {}'.format(rank,len(finalists),j+1,len(grid),row['label']))
                value = evaluate_condition(snapshot,designs,condition,target,region)
                if not np.isfinite(value):
                    raise ValueError('Non-finite stress score for '+row['scheme_id'])
                values.append(value)
                scenarios.append(dict(scheme_id=row['scheme_id'],rotation=row['rotation'],**condition,rmse=value))
                done+=1
            worst = int(np.argmax(values))
            summaries.append(dict(**row,nominal_rank=rank,worst_rmse=max(values),mean_rmse=float(np.mean(values)),
                                  best_rmse=min(values),conditions=len(values),
                                  **{'worst_'+k:v for k,v in grid[worst].items()}))
        ranked = rank_robustness(summaries)
        winner_designs = next(ds for r,ds in finalists if r['scheme_id']==ranked[0]['scheme_id'])
        check(done,'Drawing robustness report and saving scores')
        pdf, png = folder/'result.pdf',folder/'result.png'
        draw_report(pdf,png,ranked,scenarios,settings)
        files = [(pdf,destination),(png,destination.with_suffix('.png'))]
        for suffix,rows in (('_nominal.csv',nominal),('_robustness.csv',ranked),('_sweep.csv',scenarios)):
            path=folder/('result'+suffix); write_csv(path,rows)
            files.append((path,destination.with_name(destination.stem+suffix)))
        metadata=folder/'result.json'
        metadata.write_text(json.dumps(dict(settings=asdict(settings),factors=list(factors),
            stress_seed=ROBUSTNESS_SEED,selection='minimum worst-case RMSE; mean RMSE breaks ties',
            nominal=nominal,ranking=ranked,winner=ranked[0],winner_designs=winner_designs),indent=2))
        files.append((metadata,destination.with_suffix('.json')))
        check(done,'Publishing completed results')
        for source,target in files:
            os.replace(source,target)
    return dict(paths=[str(target) for _,target in files],ranking=ranked,winner=ranked[0])
