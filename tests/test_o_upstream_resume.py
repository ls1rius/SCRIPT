import tempfile
from pathlib import Path
import unittest

from script import controlled_memory_io as io
from script.resume_o_upstream import completed_prefix, upstream_ready


class UpstreamResumeTests(unittest.TestCase):
    def setup_paths(self, tmp):
        root = Path(tmp)
        out, parent, control = root/'o', root/'n', root/'control'
        (out/'runs/a').mkdir(parents=True)
        io.write(out/'runs/a/result.json', dict(complete=True, error=None))
        io.write(out/'experiment.json', dict(case_ids=['a','b'], parent_N=str(parent), upstream_repair_control=str(control)))
        return out, parent, control

    def test_wait_does_not_load_model_or_accept_previous_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, parent, control = self.setup_paths(tmp)
            self.assertEqual(upstream_ready(out), (False, 'waiting_for_N_repair'))
            io.write(control/'launch.json', {})
            io.write(parent/'queue_status.json', dict(state='failed', scheduler='previous'))
            self.assertEqual(upstream_ready(out), (False, 'waiting_for_N_repair'))
            io.write(parent/'queue_status.json', dict(state='running', scheduler=str(control)))
            self.assertEqual(upstream_ready(out), (False, 'waiting_for_N_source'))
            io.write(parent/'runs/b/result.json', dict(answer={'selected_option':'A'}, error=None))
            self.assertEqual(upstream_ready(out), (True, 'b'))

    def test_repaired_upstream_failure_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, parent, control = self.setup_paths(tmp)
            io.write(control/'launch.json', {})
            io.write(parent/'queue_status.json', dict(state='failed', scheduler=str(control)))
            with self.assertRaisesRegex(RuntimeError, 'Repaired N failed'):
                upstream_ready(out)

    def test_partial_and_pending_cannot_be_silently_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _, _ = self.setup_paths(tmp)
            self.assertEqual(completed_prefix(out), ['a'])
            io.write(out/'runs/a/x.pending.json', {})
            with self.assertRaisesRegex(ValueError, 'Pending'):
                completed_prefix(out)
            (out/'runs/a/x.pending.json').unlink()
            io.write(out/'runs/b/result.json', dict(complete=False, error='broken'))
            with self.assertRaisesRegex(ValueError, 'complete O/O'):
                completed_prefix(out)

    def test_hole_in_prefix_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _, _ = self.setup_paths(tmp)
            io.write(out/'runs/c/result.json', dict(complete=True, error=None))
            with self.assertRaisesRegex(ValueError, 'non-prefix'):
                completed_prefix(out)


if __name__ == '__main__':
    unittest.main()
