import tempfile
from pathlib import Path
import unittest
import json

from script import controlled_memory_io as io
from script import run_m_scene_experiment as runner


class MExperimentTests(unittest.TestCase):
    def test_revision_copies_only_sources_from_live_run_and_requires_validated_code(self):
        import shutil
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);prior=root/'prior';out=root/'new';gate=root/'gate'
            ids=[f'q{i}' for i in range(300)]
            for name,value in [('cohort.json',ids),('evaluation_only.json',{i:0 for i in ids}),
                ('qa_config.json',dict(query=dict(temporal_qa_evidence='scene_planned_script'))),('qa_expected_artifacts.json',{}),
                ('experiment.json',dict(version='old',prior_runtime_seconds=123.))]:
                io.write(prior/name,value)
            for uid in ids:io.write(prior/'inputs'/f'{uid}.json',dict(original_source=uid))
            io.write(prior/'frozen_hashes.json',{str(p.relative_to(prior)):io.sha(p)
                for p in prior.rglob('*') if p.is_file()})
            io.write(prior/'queue_status.json',dict(state='running'))
            io.write(prior/'report.json',dict(completed=1))
            io.write(prior/'runs/q0/result.json',dict(answer=dict(selected_option='0')))
            for name in ('report.json','cases.json','experiment.json','frozen_hashes.json','semantic_review.json'):
                io.write(gate/name,dict(complete=True,passed=True) if name in ('report.json','semantic_review.json') else {})
            for name in ('m_candidate_selection.py','backend.py','prompts.py','schemas.py'):
                target=gate/'runtime/script_core'/name;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(runner.ROOT/'script_core'/name,target)
            before={str(p.relative_to(prior)):io.sha(p) for p in prior.rglob('*') if p.is_file()}
            runner.prepare_revision(prior,out,gate)
            self.assertFalse((out/'runs').exists())
            self.assertEqual(io.read(out/'report.json')['pending'],300)
            self.assertEqual(io.read(out/'experiment.json')['reused_benchmark_answers'],0)
            self.assertNotIn('prior_runtime_seconds',io.read(out/'experiment.json'))
            self.assertEqual(before,{str(p.relative_to(prior)):io.sha(p) for p in prior.rglob('*') if p.is_file()})
            nout=root/'N'
            runner.prepare_revision(prior,nout,gate,variant='N')
            self.assertEqual(io.read(nout/'qa_config.json')['query']['temporal_qa_evidence'],'role_bound_scene_script')
            self.assertEqual(io.read(nout/'experiment.json')['runner_entry'],'script/run_n_scene_experiment.py')
            self.assertEqual(io.read(nout/'report.json')['experiment_label'],'N')
            self.assertFalse((nout/'runs').exists())
            (gate/'runtime/script_core/m_candidate_selection.py').write_text('# altered')
            with self.assertRaisesRegex(ValueError,'code mismatch'):
                runner.prepare_revision(prior,root/'invalid',gate)

    def test_new_method_archives_old_answers_and_charges_successful_and_failed_calls(self):
        from script_core.m_scene_memory import VERSION
        with tempfile.TemporaryDirectory() as td:
            prior=Path(td)/'prior';out=Path(td)/'next'
            io.write(prior/'cohort.json',['good','bad'])
            io.write(prior/'evaluation_only.json',dict(good=0,bad=1))
            io.write(prior/'inputs/good.json',dict(question='immutable source'))
            for name in ['script','script_core']:
                p=prior/'runtime'/name/'old.py';p.parent.mkdir(parents=True);p.write_text('# frozen\n')
            io.write(prior/'experiment.json',dict(version='old-method',cpu_preparation_seconds=.4,
                prior_preparation_seconds=2.,prior_runtime_seconds=12.,prior_failed_query_seconds=5.,
                prior_archived_successful_queries=2))
            io.write(prior/'frozen_hashes.json',{str(p.relative_to(prior)):io.sha(p)
                for p in prior.rglob('*') if p.is_file()})
            io.write(prior/'queue_status.json',dict(state='failed'))
            io.write(prior/'report.json',dict(completed=2,successful=1,errors=1))
            io.write(prior/'setup.json',dict(seconds=2.))
            io.write(prior/'functional/cost_summary.json',dict(seconds=3.))
            io.write(prior/'launch.json',dict(worker_pid=999999998))
            io.write(prior/'controller_launch.json',dict(pid=999999999))
            for uid,seconds in [('good',4.),('bad',6.)]:
                io.write(prior/'runs'/uid/'result.json',dict(answer=dict(selected_option='0') if uid=='good' else None,
                    error=None if uid=='good' else dict(message='failed')))
                p=prior/'runs'/uid/'partial_temporal/calls.jsonl';p.parent.mkdir(parents=True,exist_ok=True)
                p.write_text(json.dumps(dict(operation='m_answer',actual_cost=seconds))+'\n')
            before={str(p.relative_to(prior)):io.sha(p) for p in prior.rglob('*') if p.is_file()}
            runner.prepare_after_failed_gate(prior,out)
            policy=io.read(out/'experiment.json')
            self.assertEqual(policy['version'],VERSION)
            self.assertEqual(policy['prior_runtime_seconds'],27.)
            self.assertEqual(policy['prior_failed_query_seconds'],11.)
            self.assertEqual(policy['prior_archived_query_seconds'],15.)
            self.assertEqual(policy['prior_archived_successful_queries'],3)
            self.assertFalse((out/'runs').exists())
            self.assertEqual(io.read(out/'report.json')['completed'],0)
            self.assertEqual(before,{str(p.relative_to(prior)):io.sha(p) for p in prior.rglob('*') if p.is_file()})

    def test_report_keeps_pending_separate_and_charges_failed_calls(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            io.write(out/'cohort.json',['ok','failed','pending'])
            io.write(out/'evaluation_only.json',dict(ok=2,failed=0,pending=0))
            io.write(out/'runs/ok/result.json',dict(answer=dict(selected_option='2'),error=None))
            io.write(out/'runs/ok/partial_search/result.json',dict(opened_scenes=['S2']))
            io.write(out/'runs/failed/failure.json',dict(answer=None,error=dict(message='overflow')))
            for uid, op, seconds in [('ok','m_answer',.4),('failed','m_evidence_plan',12.)]:
                path = out/'runs'/uid/'partial_temporal/calls.jsonl'
                path.parent.mkdir(parents=True,exist_ok=True)
                import json
                path.write_text(json.dumps(dict(operation=op,actual_cost=seconds,input_tokens=123,
                    output_tokens=4,error=None if uid=='ok' else dict(message='overflow')))+'\n')
            report = runner.report(out)
            self.assertEqual((report['completed'],report['successful'],report['errors'],report['pending']),
                             (2,1,1,1))
            self.assertEqual(report['accuracy_among_completed'],.5)
            self.assertAlmostEqual(report['measured_query_seconds'],12.4)
            self.assertEqual(report['mean_input_tokens'],123)
            self.assertFalse(report['complete'])

    def test_frozen_snapshot_tampering_refused(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            io.write(out/'inputs/q.json',dict(question='source'))
            io.write(out/'frozen_hashes.json',{'inputs/q.json':io.sha(out/'inputs/q.json')})
            runner.verify(out)
            io.write(out/'inputs/q.json',dict(question='changed'))
            with self.assertRaisesRegex(ValueError,'Frozen M artifact'):
                runner.verify(out)

    def test_report_exposes_semantic_failure_even_when_technical_contracts_pass(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            io.write(out/'cohort.json',['pending'])
            io.write(out/'evaluation_only.json',dict(pending=0))
            io.write(out/'validation/functional.json',dict(passed=True,
                semantic_navigation_probe_passed=False,
                semantic_diagnostics=[dict(case='comparison',passed=False,opened=['S1'])],
                scope='Technical contracts only'))
            result=runner.report(out)
            self.assertTrue(result['functional_technical_contracts_passed'])
            self.assertFalse(result['semantic_navigation_probe_passed'])
            self.assertFalse(result['semantic_diagnostics'][0]['passed'])
            self.assertEqual(result['completed'],0)


if __name__=='__main__':
    unittest.main()
