from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from script import controlled_memory_io as io
from script import run_p_full_experiment as full
from script import run_p_sentence_experiment as single
from tests import test_p_sentence_filter as fixtures
FakeBackend=fixtures.FakeBackend
from tests import test_p_full_experiment as full_fixtures
from script_core import p_sentence_filter as p


class ContextResumeTests(unittest.TestCase):
    def test_completed_selection_skips_all_preprocessing_and_charges_dependency(self):
        cfg,q,s,original=fixtures.PTests().setup_case()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old=root/'old';single.run_case(FakeBackend(),cfg,q,s,original,old)
            decisions=io.read(old/'selection.json');backend=FakeBackend()
            with patch('script.resume_p_context.validated_selection',return_value=decisions):
                result=single.run_case(backend,cfg,q,s,original,root/'new',selection_replay=dict(folder=str(old),successful_seconds=17.))
            self.assertEqual([r['operation'] for op,r in backend.calls if op==p.BATCH],[p.REVIEW])
            self.assertEqual([op for op,r in backend.calls],[p.BATCH,p.ANSWER])
            self.assertEqual(result['cost_summary']['shared_dependency_seconds'],17.)
            self.assertEqual(result['inherited_selection_seconds'],17.)
            self.assertEqual(io.read(old/'selection.json'),io.read(root/'new/selection.json'))

    def test_selection_mismatch_prevents_model_calls(self):
        cfg,q,s,original=fixtures.PTests().setup_case();backend=FakeBackend()
        with tempfile.TemporaryDirectory() as td,patch('script.resume_p_context.validated_selection',side_effect=ValueError('changed')):
            with self.assertRaisesRegex(ValueError,'changed'):
                single.run_case(backend,cfg,q,s,original,Path(td)/'new',selection_replay=dict(folder=td,successful_seconds=17.))
            self.assertEqual(backend.calls,[])

    def test_failed_review_cost_still_consumes_original_query_budget(self):
        cfg,q,s,original=fixtures.PTests().setup_case()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old=root/'old';single.run_case(FakeBackend(),cfg,q,s,original,old)
            decisions=io.read(old/'selection.json');backend=FakeBackend()
            with patch('script.resume_p_context.validated_selection',return_value=decisions):
                result=single.run_case(backend,cfg,q,s,original,root/'new',selection_replay=dict(
                    folder=str(old),successful_seconds=17.,prior_failed_seconds=3.))
            self.assertEqual([op for op,r in backend.calls],[p.BATCH,p.ANSWER])
            self.assertEqual(result['inherited_selection_seconds'],17.)
            self.assertEqual(result['prior_failed_processing_seconds'],3.)
            cost=result['cost_summary']
            self.assertEqual(cost['shared_dependency_seconds'],20.)
            self.assertAlmostEqual(cost['budgeted_total'],20.+cost['total'])
            self.assertEqual(cost['total_cap'],3600)

    def test_resumed_success_cost_is_inherited_but_not_known_demo(self):
        helper=full_fixtures.FullPTests()
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);helper.setup_run(out);helper.result(out,'new',1,7)
            policy=io.read(out/'experiment.json');policy['resumed_success_case_ids']=['new'];policy['historical_failed_call_seconds']=.25
            io.write(out/'experiment.json',policy);r=full.report(out)
            self.assertEqual(r['new_processing_seconds'],0)
            self.assertEqual(r['inherited_successful_processing_seconds'],17)
            self.assertEqual(r['subgroups']['known_demos']['completed'],1)
            self.assertEqual(r['subgroups']['additional']['completed'],1)
            self.assertEqual(r['historical_failed_call_seconds'],.25)

    def test_version_groups_keep_inherited_and_new_methods_distinct(self):
        helper=full_fixtures.FullPTests()
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);helper.setup_run(out);helper.result(out,'new',1,7)
            for uid,version in [('old','v15'),('new','v16')]:
                r=io.read(out/'runs'/uid/'result.json');r['version']=version;io.write(out/'runs'/uid/'result.json',r)
            policy=io.read(out/'experiment.json');policy['version']='v16';policy['historical_failed_attempts']=[dict(processing_seconds=4.)]
            io.write(out/'experiment.json',policy);r=full.report(out)
            self.assertEqual(r['version_subgroups'],{'v15':{'completed':1,'correct':1},'v16':{'completed':1,'correct':1}})
            self.assertEqual(r['historical_failed_processing_seconds'],4.)

if __name__=='__main__':unittest.main()
