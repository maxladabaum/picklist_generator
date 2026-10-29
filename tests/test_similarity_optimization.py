import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from PIL import Image, PdfParser

from alignment_reconstruction import AlignmentSettings
from exhaustive_pdf import ReportSettings, ExportCancelled, generate_views
from image_encoding import CHANNELS, geometry
from similarity_optimization import conditions, export_optimization, evaluate_condition
import test_image_encoding


class RobustnessTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.folder=Path(folder.name)
        self.image=self.folder/'source.png'; Image.new('RGB',(40,60),(100,150,200)).save(self.image)
        self.settings=ReportSettings(str(self.image),'Fit with black padding',50,{c:5 for c in CHANNELS},7.5,1,
                                     alignment=AlignmentSettings(1,2,3,2))

    def test_cartesian_grid_and_zero_errors(self):
        grid=conditions(self.settings)
        self.assertEqual(len(grid),256)
        self.assertEqual({r['translation_x_nm'] for r in grid},{.5,1,1.5,2})
        self.assertEqual({r['translation_y_nm'] for r in grid},{1,2,3,4})
        self.assertEqual({r['rotation_deg'] for r in grid},{1.5,3,4.5,6})
        self.assertEqual({r['fwhm_nm'] for r in grid},{3.75,7.5,11.25,15})
        zero=replace(self.settings,alignment=AlignmentSettings(0,0,0,2))
        self.assertEqual(len(conditions(zero)),4)

    def test_shortlist_frozen_assignments_minimax_and_exports(self):
        keys=['fixed_4x4','joint_6x3','joint_7x3']
        frozen={key:[dict(marker=key,fraction=.37)] for key in keys}
        def nominal(settings,key,rotation,**kwargs):
            self.assertTrue(settings.optimize); self.assertTrue(settings.alignment_enabled)
            self.assertEqual(kwargs['score_only'],'assignments')
            info=geometry(key,rotation)
            return dict(scheme_id=key,label=info['encoding_label'],rotation=rotation,
                        design_count=info['design_count'],scoring_width=16,scoring_height=24,
                        final_rmse=.1*(keys.index(key)+1)),frozen[key]
        def stress(settings,designs,condition,target,region):
            key=designs[0]['marker']
            self.assertIs(designs,frozen[key])
            self.assertEqual(designs[0]['fraction'],.37)
            return condition['translation_x_nm'] if key==keys[0] else .6
        output=self.folder/'robustness.pdf'; progress=[]
        with patch('similarity_optimization.generate_views',side_effect=nominal) as generate, \
             patch('similarity_optimization.evaluate_condition',side_effect=stress) as evaluate:
            result=export_optimization(output,self.settings,schemes=keys,top_n=2,
                                       progress=lambda d,t,p:progress.append((d,t,p)))
        self.assertEqual(generate.call_count,3)
        self.assertEqual(evaluate.call_count,512)
        self.assertEqual(result['winner']['scheme_id'],'joint_6x3')
        self.assertEqual(result['ranking'][1]['worst_rmse'],2)
        self.assertEqual(progress[-1][:2],(515,515))
        with PdfParser.PdfParser(filename=str(output)) as pdf:
            self.assertEqual(len(pdf.pages),3)
        meta=json.loads(output.with_suffix('.json').read_text())
        self.assertEqual(meta['winner_designs'],frozen['joint_6x3'])
        with output.with_name('robustness_sweep.csv').open() as f:
            self.assertEqual(len(list(csv.DictReader(f))),512)
        self.assertFalse(self.settings.optimize)
        old=output.read_bytes()
        with self.assertRaises(ExportCancelled):
            export_optimization(output,self.settings,cancelled=lambda:True)
        self.assertEqual(old,output.read_bytes())

    def test_real_frozen_assignments_are_scored_at_changed_errors(self):
        from similarity_optimization import ROBUSTNESS_SEED
        import numpy as np
        settings=replace(self.settings,optimize=True,alignment_enabled=True)
        row,designs=generate_views(settings,'fixed_4x4',90,score_only='assignments')
        original=json.dumps(designs)
        target=np.zeros((24,16,3))
        for condition in (conditions(settings)[0],conditions(settings)[-1]):
            value=evaluate_condition(settings,designs,condition,target,(0,0,1,1))
            self.assertTrue(np.isfinite(value))
        self.assertEqual(json.dumps(designs),original)
        self.assertNotIn(ROBUSTNESS_SEED,(1729,2718))


class RobustnessWorkflowTests(unittest.TestCase):
    setUp=test_image_encoding.ImageWorkflowTests.setUp

    def test_button_uses_snapshot_and_separate_progress_window(self):
        view=self.view
        view.image_path.set(str(self.image_path)); view._load()
        original=view.designs
        with patch('rgb_image_view.filedialog.asksaveasfilename',return_value=str(self.folder/'robustness.pdf')), \
             patch('exhaustive_pdf.start_export_dialog') as start:
            view.similarity_optimization_button.invoke()
        self.assertTrue(start.call_args.kwargs['robustness'])
        self.assertIs(start.call_args.kwargs['button'],view.similarity_optimization_button)
        self.assertEqual(start.call_args.args[2].alignment,view._alignment_settings())
        self.assertEqual(start.call_args.args[2].fwhm_nm,float(view.gaussian_fwhm.get()))
        self.assertIs(view.robustness_export_window,start.return_value)
        self.assertIs(view.designs,original)
