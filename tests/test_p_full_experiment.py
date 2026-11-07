from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
from script import controlled_memory_io as io
from script import run_p_full_experiment as full


class FullPTests(unittest.TestCase):
    def setup_run(self,out):
        io.write(out/'experiment.json',dict(version='P-test',case_ids=['old','new'],
            inherited_case_ids=['old'],parent_N='N',scope='test'))
        io.write(out/'evaluation_only.json',dict(old=0,new=1))
        for q in ['old','new']:
            for name,value in [('question',dict(question_id=q)),('sentences',[]),('original_qa',{})]:
                io.write(out/'cases'/q/(name+'.json'),value)
        self.result(out,'old',0,10)

    def result(self,out,q,answer,cost,complete=True):
        io.write(out/'runs'/q/'result.json',dict(question_id=q,complete=complete,
            error=None if complete else {'message':'failure'},answer=dict(selected_option=str(answer)),
            input_tokens=10,answer_seconds=.2,reader_review_seconds=2.,reader_total_seconds=2.2,
            cost_summary=dict(total=cost)))

    def test_inherited_cost_once_and_failure_not_wrong_answer(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);self.setup_run(out);self.result(out,'new',0,7,False)
            r=full.report(out)
            self.assertEqual((r['completed'],r['correct'],r['errors']),(1,1,1))
            self.assertEqual(r['new_processing_seconds'],7)
            self.assertEqual(r['inherited_successful_processing_seconds'],10)
            self.assertEqual(r['subgroups']['additional']['completed'],0)

    def test_skip_only_declared_inherited_and_run_new_once(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);self.setup_run(out);backend=Mock();lease=MagicMock()
            def run(*args,**kwargs):self.result(out,'new',1,12)
            with patch.object(full.source,'prepare_case',return_value=True),patch.object(full.source,'verify_case'), \
                 patch.object(full.source,'reload_after_source_wait',return_value=lease) as load, \
                 patch('script.p_repair_validation.run') as gate,patch.object(full.single,'run_case',side_effect=run) as case:
                full.run_live(out,backend,{})
            case.assert_called_once();gate.assert_called_once();load.assert_called_once()
            self.assertEqual(case.call_args.args[2]['question_id'],'new')
            self.assertNotIn('frozen',case.call_args.kwargs)
            lease.__exit__.assert_called_once()
            self.assertTrue(full.report(out)['complete'])

    def test_wait_closes_model_before_waiting_and_reacquires(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);self.setup_run(out);backend=Mock();lease=MagicMock();events=[]
            backend.close.side_effect=lambda **kw:events.append('close')
            with patch.object(full.source,'prepare_case',side_effect=[False,True]),patch.object(full.source,'verify_case'), \
                 patch.object(full.source,'assert_upstream_running'),patch.object(full.time,'sleep',side_effect=lambda _:events.append('wait')), \
                 patch.object(full.source,'reload_after_source_wait',side_effect=lambda *a:events.append('load') or lease), \
                 patch('script.p_repair_validation.run'),patch.object(full.single,'run_case',side_effect=lambda *a:self.result(out,'new',1,12)):
                full.run_live(out,backend,{})
            self.assertEqual(events[:3],['close','wait','load'])
            lease.__exit__.assert_called_once()

    def test_failed_gate_stops_before_case_and_releases_gpu(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);self.setup_run(out);backend=Mock();lease=MagicMock()
            with patch.object(full.source,'prepare_case',return_value=True),patch.object(full.source,'verify_case'), \
                 patch.object(full.source,'reload_after_source_wait',return_value=lease), \
                 patch('script.p_repair_validation.run',side_effect=ValueError('gate')),patch.object(full.single,'run_case') as case:
                with self.assertRaisesRegex(ValueError,'gate'):full.run_live(out,backend,{})
            case.assert_not_called();lease.__exit__.assert_called_once()

    def test_preexisting_attempt_is_never_retried(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);self.setup_run(out);(out/'runs/new').mkdir()
            with patch.object(full.source,'prepare_case') as prep:
                with self.assertRaisesRegex(ValueError,'already attempted'):full.run_live(out,Mock(),{})
            prep.assert_not_called()


if __name__=='__main__':unittest.main()
