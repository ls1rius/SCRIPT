import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from script.parallel_g_rl_memories import partition, worker


class ParallelGMemoryTests(unittest.TestCase):
    def test_disjoint_balanced_partition(self):
        weights = dict(a=12, b=11, c=10, d=9, e=8, f=7, g=6, h=5)
        groups = partition(list(weights), weights, 4)
        self.assertEqual(sorted(u for group in groups for u in group), sorted(weights))
        self.assertEqual([sum(weights[u] for u in group) for group in groups], [17]*4)

    def test_wrapper_only_selects_shard_and_isolates_progress_setup(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            (out/'parallel').mkdir()
            (out/'parallel/assignment.json').write_text(json.dumps({'shards': [['b']]}))
            (out/'cohort.json').write_text(json.dumps(['a', 'b']))
            saved = []
            raw_read = lambda path: json.loads(Path(path).read_text())
            raw_write = lambda path, value: saved.append((path, value))
            raw_warm = lambda root, mode, label: label
            io = SimpleNamespace(read=raw_read, write=raw_write)
            protocol = SimpleNamespace(warm_backend=raw_warm)
            builder = SimpleNamespace(read=raw_read)

            def fake_worker(root):
                self.assertEqual(builder.read(root/'cohort.json'), ['b'])
                self.assertEqual(io.read(root/'cohort.json'), ['a', 'b'])
                io.write(root/'progress.json', {'completed': 1})
                io.write(root/'materialized/b/sealed.json', {'hash': 'abc'})
                self.assertEqual(protocol.warm_backend(root, 'qa', 'G_train_200'), 'G_train_200_shard_0')
                return 0

            builder.worker = fake_worker
            old_path = list(sys.path)
            try:
                with patch.dict(sys.modules, build_g_rl_memories=builder,
                                controlled_memory_io=io, compact_protocol=protocol):
                    self.assertEqual(worker(out, 0), 0)
            finally:
                sys.path[:] = old_path
            self.assertEqual(saved[0][0], out/'parallel/progress-0.json')
            self.assertEqual(saved[1][0], out/'materialized/b/sealed.json')
            self.assertIs(builder.read, raw_read)
            self.assertIs(io.write, raw_write)
            self.assertIs(protocol.warm_backend, raw_warm)


if __name__ == '__main__':
    unittest.main()
