import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from script import restrict_q6_grpo_gpu0 as control
from script.scale_live_parallel_pool import write


class GPU0RestrictionTests(unittest.TestCase):
    def test_holding_and_restoring_never_changes_running_or_complete_results(self):
        jobs={'a':dict(state='pending',seed=1),'b':dict(state='running',worker_id=2,seed=2),
              'c':dict(state='complete',worker_id=2,result={'sha256':'original'})}
        held=control.hold_pending(jobs)
        self.assertEqual(held['a']['state'],'held_for_gpu0_release')
        self.assertEqual(held['b'],jobs['b']);self.assertEqual(held['c'],jobs['c'])
        self.assertEqual(control.restore_pending(held),jobs)
        self.assertEqual(jobs['a']['state'],'pending')

    def test_running_trajectory_prevents_any_signal_or_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);write(root/'jobs.json',{'a':dict(state='running',worker_id=2)})
            with patch.object(control.os,'kill') as kill:
                self.assertIsNone(control.retire_if_idle(root,dict(id=2,pid=123,identity='x')))
                kill.assert_not_called()

    def test_only_gpu0_scope_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);write(root/'config.json',dict(allowed_gpu_indices=['0','2'],learner={'id':0},runtime=str(root)))
            with self.assertRaisesRegex(ValueError,'GPU0'):control.configure(root)

    def test_loading_or_gate_inflight_is_not_interrupted(self):
        for stage in ['loading','actor_gate']:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);write(root/'jobs.json',{})
                write(root/'workers/2/status.json',{'stage':stage})
                with patch.object(control,'process_alive',return_value=True),patch.object(control.os,'kill') as kill:
                    self.assertIsNone(control.retire_if_idle(root,dict(id=2,pid=123,identity='x')))
                    kill.assert_not_called()


if __name__=='__main__':unittest.main()
