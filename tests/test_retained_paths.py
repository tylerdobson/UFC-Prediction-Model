import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ufc_odds_model.research_evaluation import _intact_payload


class RetainedPathTests(unittest.TestCase):
    def test_missing_repo_path_requires_the_original_hash_and_existing_path_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root/'data/raw/research_complete/receipt.json'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'original')
            sha = hashlib.sha256(b'original').hexdigest()
            with patch('pathlib.Path.cwd', return_value=root):
                self.assertTrue(_intact_payload('/missing/project/data/raw/research_complete/receipt.json', sha))
                self.assertFalse(_intact_payload('/missing/project/data/raw/research_complete/receipt.json', '0'*64))
                self.assertFalse(_intact_payload('/missing/other/receipt.json', sha))
                existing = root/'old/data/raw/research_complete/receipt.json'
                existing.parent.mkdir(parents=True)
                existing.write_bytes(b'changed')
                self.assertFalse(_intact_payload(str(existing), sha))
