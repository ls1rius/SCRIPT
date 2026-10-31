from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from script import controlled_memory_io as io
from script import run_compact_evidence_experiment as compact
from script import run_controlled_memory_experiment as protocol
from script.run_compact_resident import drain, drain_many


class CompactResidentTests(unittest.TestCase):
    def test_two_experiments_share_one_backend_without_changing_qa_configs(self):
        with tempfile.TemporaryDirectory() as td:
            backend=Mock()
            shared=SimpleNamespace(warm_backend=Mock(return_value=({},backend,object(),object())))
            original=shared.warm_backend
            tasks=[];used=[]
            for name,count in [('C',1),('D',2)]:
                root=Path(td)/name
                for filename in ['qa_config.json','qa_expected_artifacts.json','control/compact_protocol.py']:
                    path=root/filename;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('same')
                run=SimpleNamespace(io=io,prepare_ready=Mock(),next_range=Mock(side_effect=[(i,i+1) for i in range(count)]),
                    report=Mock(side_effect=[dict(complete=False)]*count+[dict(complete=True)]*3))

                def work(out,start,end,phase):
                    prepared=shared.warm_backend(out,'qa',phase)
                    used.append(prepared[1]);prepared[1].close()
                    return 0

                run.worker=Mock(side_effect=work);tasks.append((root,run))
            self.assertEqual(drain_many(tasks,shared),0)
            original.assert_called_once()
            self.assertEqual(len(used),3)
            self.assertTrue(all(b is used[0] for b in used))
            backend.close.assert_called_once()
            self.assertIs(shared.warm_backend,original)

    def scenario(self, spans, code=0, error=None):
        backend = Mock()
        protocol = SimpleNamespace(warm_backend=Mock(return_value=({}, backend, object(), object())))
        real_warm = protocol.warm_backend
        io = SimpleNamespace(write=Mock(), now=lambda: 'now')
        run = SimpleNamespace(io=io, prepare_ready=Mock(),
            report=Mock(side_effect=[dict(complete=False)]*len(spans)+[dict(complete=True)]),
            next_range=Mock(side_effect=spans))
        used = []

        def work(out, start, end, phase):
            cached = protocol.warm_backend(out, 'qa', phase)
            used.append(cached[1])
            try:
                if error:
                    raise error
                return code
            finally:
                cached[1].close()

        run.worker = Mock(side_effect=work)
        return run, protocol, backend, real_warm, used

    def test_model_survives_range_boundary_and_new_evidence_wait(self):
        run, protocol, backend, warm, used = self.scenario([(0,40), None, (40,80)])
        with patch('script.run_compact_resident.time.sleep') as sleep:
            self.assertEqual(drain(Path('/run'), run, protocol), 0)
        self.assertEqual(warm.call_count, 1)
        self.assertIs(used[0], used[1])
        backend.close.assert_called_once_with()
        sleep.assert_called_once_with(5)
        states=[c.args[1] for c in run.io.write.call_args_list]
        self.assertTrue(states[1]['model_resident'])
        self.assertEqual(states[1]['state'], 'waiting_for_parent_inputs')
        self.assertIs(protocol.warm_backend, warm)

    def test_failed_call_stops_without_retry_and_closes_model(self):
        run, protocol, backend, warm, _ = self.scenario([(0,40), (40,80)], code=2)
        self.assertEqual(drain(Path('/run'), run, protocol), 2)
        self.assertEqual(run.worker.call_count, 1)
        backend.close.assert_called_once_with()
        self.assertIs(protocol.warm_backend, warm)

    def test_exception_closes_model_and_restores_original_worker(self):
        run, protocol, backend, warm, _ = self.scenario([(0,40)], error=ValueError('bad seal'))
        with self.assertRaisesRegex(ValueError, 'bad seal'):
            drain(Path('/run'), run, protocol)
        backend.close.assert_called_once_with()
        self.assertIs(protocol.warm_backend, warm)

    def test_real_qa_loop_retains_backend_and_skips_finished_answers(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            ids = [f'q{i}' for i in range(41)]
            io.write(out/'cohort.json', ids)
            io.write(out/'historical_only.json', {})
            io.write(out/'evaluation_only.json', {u:'0' for u in ids})
            io.write(out/'experiment.json', dict(parent=str(out/'parent')))
            io.write(out/'frozen_hashes.json', {})
            inputs = dict(head='system', prompts=['  Description: evidence\n\n###\n\n Questions: Q'])

            def ready(uid):
                for arm in compact.ARMS:
                    io.write(out/'final_inputs'/arm/f'{uid}.json', inputs)
                io.write(out/'input_seals'/f'{uid}.json', dict(
                    inputs={a:io.sha(out/'final_inputs'/a/f'{uid}.json') for a in compact.ARMS},
                    inherited_extraction_seconds=10))
                io.write(out/'packets'/f'{uid}.json', dict(packet=dict(question=dict(options=['yes','no']))))

            for uid in ids[:40]:
                ready(uid)
            for arm in compact.ARMS:
                io.write(out/'runs'/arm/'q0/result.json', dict(
                    selected_option='0', error=None, seconds=1, input_tokens=10))
            backend = Mock()
            calls = []

            class Ledger:
                def __init__(self, *args, **kwargs):
                    pass

                def summary(self):
                    return {}

            class Meter:
                def __init__(self, backend, *args):
                    self.backend = backend

                def call(self, operation, inputs, **kwargs):
                    self.backend.call(operation)
                    calls.append(operation)
                    if operation == 'temporal_qa_check':
                        return dict(input_tokens=10), None
                    return dict(data=dict(selected_option='0', text='prediction: A')), None

                def close(self):
                    pass

            warm = Mock(return_value=(dict(budget=dict(online_cap=100,final_answer_reserve=10)),
                                      backend, Ledger, Meter))

            def evidence_arrives(seconds):
                backend.close.assert_not_called()
                ready(ids[40])

            with patch.object(protocol, 'warm_backend', warm), \
                    patch.dict('sys.modules', compact_protocol=protocol), \
                    patch.object(protocol.io, 'ARMS', compact.ARMS), \
                    patch.object(protocol.io, 'ORDERS', (compact.ARMS,)*6), \
                    patch('script.run_compact_resident.time.sleep', side_effect=evidence_arrives), \
                    patch('builtins.print'):
                self.assertEqual(drain(out, compact, protocol), 0)
                self.assertEqual(compact.report(out)['common_completed'], 41)
            warm.assert_called_once()
            backend.close.assert_called_once()
            self.assertEqual(calls.count('videotree_qa'), 40*3)


if __name__ == '__main__':
    unittest.main()
