"""Publication recovery must preserve completed calls and never restart RL."""
from concurrent.futures import ThreadPoolExecutor
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import contextmanager
import types

from script import resume_q6_evaluation as recovery, run_q_experiment as base
from script.scale_live_parallel_pool import read, write, sha
from script_core.config import digest
from script_core import q_evidence as q


class EvaluationRecoveryTests(unittest.TestCase):
    def fixture(self, root, uid='saved'):
        target = root/'evaluation/attempts'/uid
        case = root/'evaluation/cases'/uid
        question = dict(question_id=uid, question='What happens?', options=['Open.', 'Close.'])
        original = dict(query_id=uid, head='Choose.', prompts=['  Description: old\n\n###\n\n Questions: What happens? \n Options: \n A: Open. \n B: Close.'])
        write(case/'question.json', question)
        write(case/'original_qa.json', original)
        write(target/'sources.json', [])
        write(target/'retained.json', [])
        write(target/'selection.json', {})
        (target/'calls.jsonl').write_text('')
        arms = {a:dict(answer=dict(selected_option='0'), retained_evidence_hash=digest([])) for a in ['Q', 'Qplus']}
        for arm in arms:
            write(target/arm/'final_input.json', q.final_input(original, [], arm == 'Qplus'))
        write(target/'result.json', dict(question_id=uid, complete=True, error=None, arms=arms,
              retained_evidence_hash=digest([]), cost_summary=dict(total=0, budget_violation=False)))
        return target

    def test_publish_initializes_missing_runs_and_keeps_original_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self.fixture(root)
            write(root/'evaluation/claims.json', {'saved':dict(state='running')})
            before = sha(target/'result.json')
            self.assertFalse((root/'evaluation/runs').exists())
            base.publish(root/'evaluation', 'saved', target)
            self.assertTrue(target.is_symlink())
            self.assertEqual(sha(target/'result.json'), before)
            self.assertEqual(read(root/'evaluation/claims.json')['saved']['state'], 'complete')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                base.publish(root/'evaluation', 'saved', target)

    def test_concurrent_publishers_create_parent_once_without_lost_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ids = [str(i) for i in range(8)]
            for uid in ids:
                self.fixture(root, uid)
            write(root/'evaluation/claims.json', {uid:dict(state='running') for uid in ids})
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(lambda uid:base.publish(root/'evaluation', uid, root/'evaluation/attempts'/uid), ids))
            self.assertTrue(all(row['state'] == 'complete' for row in read(root/'evaluation/claims.json').values()))
            self.assertEqual(len(list((root/'evaluation/runs').iterdir())), 8)

    def test_incomplete_result_is_never_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self.fixture(root)
            row = read(target/'result.json')
            row['complete'] = False
            write(target/'result.json', row)
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                recovery.publish(root, 'saved', target)
            self.assertTrue(target.is_dir())

    def test_audit_rejects_pending_calls_and_reader_evidence_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self.fixture(root)
            recovery.audit_result(root, 'saved', target)
            write(target/'raw_calls/one.pending.json', {})
            with self.assertRaisesRegex(ValueError, 'pending'):
                recovery.audit_result(root, 'saved', target)
            (target/'raw_calls/one.pending.json').unlink()
            row = read(target/'result.json')
            row['arms']['Qplus']['retained_evidence_hash'] = 'changed'
            write(target/'result.json', row)
            with self.assertRaisesRegex(ValueError, 'Reader evidence'):
                recovery.audit_result(root, 'saved', target)

    def test_prepare_republishes_without_modifying_adapter_training_or_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'experiment'
            target = self.fixture(root)
            weights = root/'adapter/adapter.safetensors'
            weights.parent.mkdir()
            weights.write_bytes(b'completed final adapter')
            write(root/'adapter/adapter_config.json', dict(weights_sha256=sha(weights)))
            training = dict(complete=True, epochs=3, completed_groups=600, rollouts=1200, adapter=dict(weights_sha256=sha(weights)))
            for name, value in [('training/result.json', training), ('training/status.json', {}), ('training/history.json', []),
                                ('adapter_ready.json', {}), ('report.json', {}), ('frozen_hashes.json', {}),
                                ('failure.json', dict(message='missing runs parent')),
                                ('evaluation/claims.json', {'saved':dict(state='running')}),
                                ('queue_status.json', dict(state='failed', controller_pid=999999999, workers={}))]:
                write(root/name, value)
            before = {str(f.relative_to(root)):sha(f) for f in [weights, root/'training/result.json', target/'result.json']}
            control = root/'recovery'
            recovery.prepare(root, control)
            self.assertFalse((root/'failure.json').exists())
            self.assertEqual(read(control/'history/failure.json')['message'], 'missing runs parent')
            self.assertEqual(read(root/'evaluation/claims.json')['saved']['state'], 'complete')
            self.assertEqual(before, {n:sha(root/n) for n in before})
            self.assertFalse(read(control/'recovery.json')['training_restarted'])
            recovery.verify(control, read(control/'config.json'))
            weights.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Protected'):
                recovery.verify(control, read(control/'config.json'))

    def test_exact_gpu_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = Path(tmp)
            write(control/'config.json', dict(allowed_gpu_indices=['0', '1'], workers=6, runtime=str(control)))
            with self.assertRaisesRegex(ValueError, 'GPU0/2'):
                recovery.configure(control)

    def test_model_process_is_read_only_after_lazy_prepare(self):
        # ModelWorker creates its process on the first call, not in its constructor.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = root/'control'
            cfg = dict(out=str(root), runtime=str(root/'runtime'), allowed_gpu_indices=['0', '2'], workers=6,
                       minimum_free_mib=24576, stable_observations=3)
            write(control/'config.json', cfg)
            write(root/'evaluation/qa_config.json', {})
            write(root/'evaluation/claims.json', {})
            weights = root/'adapter/adapter.safetensors'
            weights.parent.mkdir()
            weights.write_bytes(b'adapter')
            class LazyBackend:
                def __init__(self, cfg):
                    self.process = None
                def call(self, operation, inputs, cap):
                    self.process = types.SimpleNamespace(pid=123)
                    return dict(ready=True)
                def close(self, **kwargs):
                    pass
            @contextmanager
            def acquired(policy, waiting):
                self.assertEqual(policy['preference'], ['0', '2'])
                yield dict(index='0', uuid='test', reservation_id='test'), None
            with patch.object(recovery, 'verify'), patch('script.gpu_queue.acquire', acquired), \
                 patch('script_core.budget.ModelWorker', LazyBackend), patch('script.q_v4_validation.validate_live'), \
                 patch('script.run_q_experiment.claim', return_value=None), patch.dict('os.environ'):
                recovery.worker(control, 0)
            self.assertEqual(read(control/'workers/0/status.json')['stage'], 'complete')


if __name__ == '__main__':
    unittest.main()
