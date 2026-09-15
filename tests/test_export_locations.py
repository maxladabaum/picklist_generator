import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from picklist_app import PicklistApp


class ExportLocationTests(unittest.TestCase):
    def test_combined_csvs_and_both_selection_pdfs_share_configured_folder(self):
        panel = {
            "group": "Test", "label": "Sample", "rows": ["Top"], "columns": ["R1"],
            "active": [("Top", "R1")], "selected": [("Top", "R1")],
            "selected_names": ["Sample_Top-R1"],
        }
        runs = [{
            "run_name": "Sample",
            "selection_panels": [panel],
            "picklist": [{"Destination Plate Name": "Plate", "Destination Well": "A01", "Transfer Volume": 50}],
            "mixing_recipe": [{"Reagent": "Water", "Volume_uL": 10}],
        }]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "exports"
            app = SimpleNamespace(
                output_root=Mock(get=Mock(return_value=str(output))),
                storage_status=Mock(),
                _selected_storage_items=lambda: runs,
                _replacement_selection_panels=lambda: [panel],
            )
            app._shared_export_directory = lambda: PicklistApp._shared_export_directory(app)
            with patch("picklist_app.messagebox.showinfo"), patch("picklist_app.messagebox.showerror") as error:
                for _ in range(2):
                    PicklistApp._generate_combined_storage(app)
                    PicklistApp._generate_storage_selections_pdf(app)
                    PicklistApp.save_selections_pdf(app)
                error.assert_not_called()
            self.assertEqual({path.name for path in output.iterdir()}, {
                "picklist_combined.csv", "mixing_recipe_01_Sample.csv",
                "stored_run_selections.pdf", "replacement_selections.pdf",
            })
            self.assertTrue(all(path.is_file() and path.stat().st_size for path in output.iterdir()))


if __name__ == "__main__":
    unittest.main()
