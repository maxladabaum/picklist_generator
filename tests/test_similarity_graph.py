import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import replace
import numpy as np
from PIL import Image

from exhaustive_pdf import ReportSettings, generate_views, ExportCancelled
from alignment_reconstruction import AlignmentSettings, VALIDATION_SEED
from image_encoding import CHANNELS
from similarity_graph import source_content_rmse, export_similarity
import test_image_encoding


class SimilarityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.source = self.folder/'source.png'
        Image.new('RGB',(80,120),(100,150,200)).save(self.source)
        self.settings = ReportSettings(str(self.source),'Fit with black padding',50,{c:5 for c in CHANNELS},7.5,1,
                                       alignment=AlignmentSettings(origami_per_design=2))

    def test_scoring_excludes_only_padding_and_penalizes_overbrightness(self):
        signal = np.zeros((20,20,3))
        signal[5:15,5:15] = 1
        result = SimpleNamespace(density=signal,metadata={'linear_rgb_gain':1})
        target = np.ones((10,10,3))
        self.assertEqual(source_content_rmse(result,target,(.25,.25,.75,.75)),0)
        self.assertGreater(source_content_rmse(result,target,(0,0,1,1)),0)
        result.density *= 2
        self.assertEqual(source_content_rmse(result,target,(.25,.25,.75,.75)),1)

    def test_real_scores_share_source_raster_and_use_independent_alignment(self):
        settings = replace(self.settings, alignment_enabled=True, optimize=True)
        rows = [generate_views(settings,key,90,score_only=True) for key in ('fixed_4x4','tiling_2_2_0_0')]
        self.assertEqual(len({(r['scoring_width'],r['scoring_height']) for r in rows}),1)
        for row in rows:
            self.assertEqual(row['evaluation_seed'],VALIDATION_SEED)
            self.assertEqual(row['evaluation'],'held-out expected alignment image')
            self.assertTrue(np.isfinite(row['final_rmse']))
        row = generate_views(self.settings,'fixed_4x4',90,score_only=True)
        self.assertEqual(row['baseline_rmse'],row['final_rmse'])

    def test_graph_exports_ranked_data_and_valid_artifacts(self):
        output = self.folder/'similarity.pdf'
        result = export_similarity(output,self.settings,schemes=['fixed_4x4','tiling_2_2_0_0'])
        self.assertEqual(len(result['ranking']),2)
        values=[row['final_rmse'] for row in result['ranking']]
        self.assertEqual(values, sorted(values))
        self.assertTrue(output.read_bytes().startswith(b'%PDF'))
        with Image.open(output.with_suffix('.png')) as png:
            self.assertGreater(png.width,1000)
        with output.with_suffix('.csv').open() as handle:
            rows=list(csv.DictReader(handle))
        self.assertEqual([r['scheme_id'] for r in rows],[r['scheme_id'] for r in result['ranking']])
        self.assertEqual(len(json.loads(output.with_suffix('.json').read_text())['ranking']),2)
        previous=output.read_bytes()
        with self.assertRaises(ExportCancelled):
            export_similarity(output,self.settings,cancelled=lambda:True)
        self.assertEqual(previous,output.read_bytes())


class SimilarityWorkflowTests(unittest.TestCase):
    setUp = test_image_encoding.ImageWorkflowTests.setUp

    def test_similarity_button_uses_current_settings_and_its_own_progress_window(self):
        view=self.view
        view.image_path.set(str(self.image_path))
        view._load()
        with patch('rgb_image_view.filedialog.asksaveasfilename',return_value=str(self.folder/'ranking.pdf')), \
             patch('exhaustive_pdf.start_export_dialog') as start:
            view.similarity_graph_button.invoke()
        start.assert_called_once()
        self.assertTrue(start.call_args.kwargs['graph'])
        self.assertIs(start.call_args.kwargs['button'],view.similarity_graph_button)
        self.assertEqual(start.call_args.args[2].regularization_strength,view._regularization_value())
        self.assertIs(view.similarity_export_window,start.return_value)
