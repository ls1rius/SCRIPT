from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from script import controlled_memory_io as io
from script import run_o_full_experiment as full
from script import o_batched_matrix as matrix
from script.o_composition_context_eval import conditions_for, report
from script_core import o_sentence_filter as core
from script_core.o_batch_generation import OPERATION
from tests import test_o_batch as fixtures


class FullRunTests(unittest.TestCase):
    def setup_matrix(self,out,empty=False):
        cfg=fixtures.MatrixTests().setup_run(out)
        policy=io.read(out/'experiment.json');policy.update(full_cohort=True,conditions=['question_only'],parent_N='upstream')
        io.write(out/'experiment.json',policy)
        if empty:io.write(out/'cases/case/sentences.json',[])
        return cfg

    def test_only_two_arms_no_options_in_evidence_steps(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);cfg=self.setup_matrix(out);backend=fixtures.BatchBackend()
            matrix.run(out,backend,cfg)
            r=io.read(out/'runs/case/result.json')
            self.assertEqual(set(r['arms']),{'O_question_only','Oplus_question_only'})
            self.assertEqual(set(r['conditions']),{'question_only'})
            for operation,request in backend.calls:
                if operation==OPERATION:
                    self.assertTrue(all('options' not in x['request'] for x in request['items']))
                elif operation in [core.EXTRACT,core.FILTER,core.FILTER_PLAN,core.COMPOSE]:
                    self.assertNotIn('options',request)
                elif operation in ['o_answer','oplus_answer']:
                    self.assertEqual(request['options'],['D','E'])
            self.assertEqual(set(report(out)['arms']),set(r['arms']))
            self.assertFalse(report(out)['diagnostic_only'])

    def test_empty_evidence_skips_all_extraction_selection_composition(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);cfg=self.setup_matrix(out,empty=True);backend=fixtures.BatchBackend()
            matrix.run(out,backend,cfg)
            self.assertEqual([op for op,_ in backend.calls],['o_answer','oplus_answer'])
            self.assertTrue(io.read(out/'runs/case/result.json')['complete'])

    def test_full_run_refuses_option_condition_but_historical_ablation_retained(self):
        with self.assertRaises(ValueError):conditions_for(dict(full_cohort=True,conditions=['question_only','question_options']))
        self.assertEqual(conditions_for({}),{'question_only':False,'question_options':True})

    def setup_parent(self,root,empty=False):
        parent=root/'N';out=root/'O';uid='q0';src=parent/'runs'/uid
        question=dict(question_id=uid,question='Who opens a drawer?',options=['D','E'])
        io.write(out/'experiment.json',dict(case_ids=[uid],parent_N=str(parent)))
        io.write(out/'questions'/f'{uid}.json',question);io.write(parent/'inputs'/f'{uid}.json',dict(question=question))
        io.write(src/'result.json',dict(answer=dict(selected_option='0'),error=None))
        io.write(src/'partial_search/evidence_bundle.json',dict(qa_input=dict(head='Answer',prompts=['  Description: old\n\n###\n\n Questions: Who?']),opened_scenes=[] if empty else ['S0'],selected_seconds=[]))
        if empty:io.write(src/'partial_temporal/N/empty_evidence.json',dict(observed_sources=[],reason='no_relevant_candidates'))
        else:io.write(src/'partial_temporal/semantic_sources.json',dict(units=[dict(id='R0',text='D opens a drawer.')]))
        return parent,out,src,uid

    def test_atomic_source_snapshot_preserves_original_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as td:
            parent,out,src,uid=self.setup_parent(Path(td))
            before={str(p):io.sha(p) for p in parent.rglob('*') if p.is_file()}
            self.assertTrue(full.prepare_case(out,uid));full.verify_case(out/'cases'/uid)
            self.assertEqual(before,{str(p):io.sha(p) for p in parent.rglob('*') if p.is_file()})
            self.assertEqual(io.read(out/'cases'/uid/'sentences.json')[0]['text'],'D opens a drawer.')
            self.assertTrue(full.prepare_case(out,uid))
            io.write(out/'cases'/uid/'sentences.json',[])
            with self.assertRaises(ValueError):full.prepare_case(out,uid)

    def test_waits_for_unfinished_source_and_rejects_failed_source(self):
        with tempfile.TemporaryDirectory() as td:
            parent,out,src,uid=self.setup_parent(Path(td))
            (src/'result.json').unlink();self.assertFalse(full.prepare_case(out,uid))
            io.write(src/'result.json',dict(error='failure'))
            with self.assertRaises(ValueError):full.prepare_case(out,uid)
            io.write(parent/'queue_status.json',dict(state='failed'))
            with self.assertRaises(RuntimeError):full.assert_upstream_running(parent)

    def test_pending_source_is_not_copied(self):
        with tempfile.TemporaryDirectory() as td:
            parent,out,src,uid=self.setup_parent(Path(td))
            io.write(src/'current.pending.json',{})
            self.assertFalse(full.prepare_case(out,uid));self.assertFalse((out/'cases'/uid).exists())

    def test_explicit_empty_source_allowed_missing_source_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent,out,src,uid=self.setup_parent(Path(td),empty=True)
            self.assertTrue(full.prepare_case(out,uid))
            self.assertEqual(io.read(out/'cases'/uid/'sentences.json'),[])
        with tempfile.TemporaryDirectory() as td:
            parent,out,src,uid=self.setup_parent(Path(td),empty=True)
            (src/'partial_temporal/N/empty_evidence.json').unlink()
            with self.assertRaisesRegex(ValueError,'Missing source'):full.prepare_case(out,uid)

    def test_changed_question_and_source_outside_cohort_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            parent,out,src,uid=self.setup_parent(Path(td))
            with self.assertRaises(ValueError):full.prepare_case(out,'foreign')
            question=io.read(out/'questions'/f'{uid}.json');question['question']='Foreign?'
            io.write(parent/'inputs'/f'{uid}.json',dict(question=question))
            with self.assertRaises(ValueError):full.prepare_case(out,uid)

    def test_wait_releases_model_then_resumes_original_question(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);io.write(out/'experiment.json',dict(case_ids=['q0'],parent_N=str(out/'N')))
            backend=fixtures.BatchBackend();closed=[]
            backend.close=lambda **kw:closed.append(kw)
            with patch.object(full,'prepare_case',side_effect=[False,True]),patch.object(full,'assert_upstream_running'),patch.object(full,'verify_case'),patch.object(full,'reload_after_source_wait',return_value=None) as reload,patch.object(full.time,'sleep'),patch.object(matrix,'run') as run:
                full.run_live(out,backend,{})
            self.assertEqual(closed,[dict(graceful=True)]);reload.assert_called_once()
            self.assertEqual(run.call_args.kwargs['case_ids'],['q0'])


if __name__=='__main__':unittest.main()

class SharedReloadTests(unittest.TestCase):
    def test_source_wait_releases_reservation_and_finally_closes_new_lease(self):
        import os
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);io.write(out/'experiment.json',dict(case_ids=['q'],parent_N='N'))
            backend=Mock();lease=__import__("unittest.mock",fromlist=["MagicMock"]).MagicMock()
            with patch.dict(os.environ,{'VIDEOAGENT_GPU_LEASE':'initial'}), \
                 patch.object(full,'prepare_case',side_effect=[False,True]), \
                 patch.object(full,'assert_upstream_running'),patch.object(full,'verify_case'), \
                 patch.object(full,'reload_after_source_wait',return_value=lease),patch.object(full.time,'sleep'), \
                 patch('script.gpu_shared_queue.release') as release,patch.object(matrix,'run'):
                full.run_live(out,backend,{})
            release.assert_called_once_with('initial')
            lease.__exit__.assert_called_once_with(None,None,None)
            self.assertEqual(backend.close.call_count,2)
