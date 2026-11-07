import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script import parallel_artifacts as artifacts


class ParallelArtifactsTests(unittest.TestCase):
    def make_source(self, root):
        source = root/'source'
        (source/'empty').mkdir(parents=True)
        (source/'nested').mkdir()
        (source/'a.bin').write_bytes(bytes(range(256))*8192)
        (source/'nested/unicode-字.json').write_text('{"ok": true}\n')
        (source/'__pycache__').mkdir()
        (source/'__pycache__/ignored.pyc').write_bytes(b'cache')
        return source

    def test_parallel_matches_serial_and_copy_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = self.make_source(root)
            expected = {p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(source.rglob('*'))
                        if p.is_file() and '__pycache__' not in p.parts}
            self.assertEqual(artifacts.tree_hashes(source), expected)
            with patch.object(artifacts, 'tree_hashes', wraps=artifacts.tree_hashes) as hashed:
                result = artifacts.copy_tree_verified(source, root/'copied', snapshot=expected)
            self.assertEqual(hashed.call_count, 1)  # Only the final source check.
            self.assertEqual(result['hashes'], expected)
            self.assertEqual(artifacts.tree_hashes(root/'copied'), expected)
            self.assertTrue((root/'copied/empty').is_dir())
            self.assertFalse((root/'copied/__pycache__').exists())
            self.assertFalse((root/'copied.staging').exists())

    def test_modified_source_is_retained_in_staging_not_published(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = self.make_source(root)
            snapshot = artifacts.tree_hashes(source)
            (source/'a.bin').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'copied bytes differ'):
                artifacts.copy_tree_verified(source, root/'copy', snapshot=snapshot)
            self.assertFalse((root/'copy').exists())
            self.assertTrue((root/'copy.staging').exists())
            with self.assertRaises(FileExistsError):
                artifacts.copy_tree_verified(source, root/'copy')

    def test_mutation_after_copy_prevents_publication(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = self.make_source(root)
            snapshot = artifacts.tree_hashes(source)
            original = artifacts._read_file
            def changing(root_path, name, destination=None):
                result = original(root_path, name, destination)
                if destination is not None and name == 'a.bin':
                    (source/'a.bin').write_bytes(b'mutated after copied bytes')
                return result
            with patch.object(artifacts, '_read_file', changing):
                with self.assertRaisesRegex(ValueError, 'source changed'):
                    artifacts.copy_tree_verified(source, root/'copy', snapshot=snapshot)
            self.assertFalse((root/'copy').exists())
            self.assertTrue((root/'copy.staging').exists())

    def test_symlinks_special_files_and_unsafe_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = self.make_source(root)
            (source/'link').symlink_to(source/'a.bin')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                artifacts.tree_hashes(source)
            (source/'link').unlink()
            (source/'dirlink').symlink_to(source/'nested', target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                artifacts.tree_hashes(source)
            (source/'dirlink').unlink()
            import os
            os.mkfifo(source/'fifo')
            with self.assertRaisesRegex(ValueError, 'special'):
                artifacts.tree_hashes(source)
            (source/'fifo').unlink()
            with self.assertRaisesRegex(ValueError, 'Invalid artifact snapshot'):
                artifacts.copy_tree_verified(source, root/'copy', snapshot={'../escape':'0'*64})
            self.assertFalse((root/'copy.staging').exists())

    def test_no_overwrite_or_recursive_copy_and_bounded_workers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = self.make_source(root)
            with self.assertRaisesRegex(ValueError, 'inside'):
                artifacts.copy_tree_verified(source, source/'copy')
            (root/'exists').mkdir()
            with self.assertRaisesRegex(ValueError, 'already exists'):
                artifacts.copy_tree_verified(source, root/'exists')
            for count in (0, 17, True, 1.5):
                with self.assertRaises(ValueError):
                    artifacts.tree_hashes(source, workers=count)


if __name__ == '__main__':
    unittest.main()
