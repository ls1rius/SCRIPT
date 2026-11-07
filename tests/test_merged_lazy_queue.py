from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script import queue_merged_lazy_after_h as queue
from tests.test_merged_lazy_experiment import parent_fixture


class MergedLazyQueueTests(unittest.TestCase):
    def setup_queue(self, root):
        parent, after, output, folder = (root/name for name in ['H','previous','I','queue'])
        parent_fixture(parent)
        io.write(parent/'queue_status.json', dict(state='prepared'))
        io.write(after/'queue_status.json', dict(state='complete'))
        queue.prepare(folder, parent, after, output)
        return parent, after, output, folder

    def test_freezes_adjacent_renderer_and_runs_h_before_i(self):
        with tempfile.TemporaryDirectory() as td:
            parent, after, output, folder = self.setup_queue(Path(td))
            self.assertFalse(output.exists())
            self.assertIn('def adjacent_intervals', (folder/'control/merged_lazy_memory.py').read_text())
            commands = []
            def launch(command, **kwargs):
                commands.append(command)
                if len(commands) == 1:
                    self.assertIn('run_generalized_lazy_experiment.py', command[2])
                    io.write(parent/'queue_status.json', dict(state='complete'))
                elif len(commands) == 2:
                    self.assertEqual(command[3], 'prepare')
                    io.write(output/'queue_status.json', dict(state='prepared'))
                else:
                    self.assertEqual(command[3], 'queue')
                    io.write(output/'queue_status.json', dict(state='complete'))
                return SimpleNamespace(pid=123, returncode=0, poll=lambda:0)
            with patch.object(queue.subprocess, 'Popen', side_effect=launch):
                self.assertEqual(queue.run(folder), 0)
            self.assertEqual(len(commands), 3)
            self.assertEqual(io.read(folder/'queue_status.json')['state'], 'complete')

    def test_failed_dependency_stops_without_starting_h_or_i(self):
        with tempfile.TemporaryDirectory() as td:
            parent, after, output, folder = self.setup_queue(Path(td))
            io.write(after/'queue_status.json', dict(state='failed'))
            with patch.object(queue.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(RuntimeError, 'Dependency failed'):
                    queue.run(folder)
            launch.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(io.read(folder/'queue_status.json')['state'], 'failed')

    def test_standalone_h_i_queue_has_no_grpo_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parent, output, folder = (root/name for name in ['H', 'I', 'queue'])
            parent_fixture(parent)
            io.write(parent/'queue_status.json', dict(state='prepared'))
            queue.prepare(folder, parent, None, output, gpu_index='0,1,2,3')
            self.assertIsNone(io.read(folder/'plan.json')['after'])
            commands = []
            def launch(command, **kwargs):
                commands.append(command)
                if len(commands) == 1:
                    self.assertIn('run_generalized_lazy_experiment.py', command[2])
                    io.write(parent/'queue_status.json', dict(state='complete'))
                elif len(commands) == 3:
                    io.write(output/'queue_status.json', dict(state='complete'))
                return SimpleNamespace(pid=123, returncode=0, poll=lambda:0)
            with patch.object(queue.subprocess, 'Popen', side_effect=launch):
                self.assertEqual(queue.run(folder), 0)
            self.assertEqual(len(commands), 3)
            self.assertEqual(io.read(folder/'queue_status.json')['state'], 'complete')

    def test_changed_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, _, folder = self.setup_queue(Path(td))
            (folder/'control/merged_lazy_memory.py').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'Frozen artifact changed'):
                queue.run(folder)


if __name__ == '__main__':
    unittest.main()
