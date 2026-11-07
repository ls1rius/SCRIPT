from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from script import parallel_scheme_pool as pool


class SchemePoolTests(unittest.TestCase):
    def setup_pool(self, root, scheme='P'):
        out=root/'out';control=out/'parallel'
        (out/'runs').mkdir(parents=True);control.mkdir()
        pool.write(out/'cohort.json',['q0','q1','q2'])
        pool.write(control/'claims.json',{})
        if scheme!='M':
            parent=root/'N'
            pool.write(out/'experiment.json',dict(parent_N=str(parent)))
            for q in ['q0','q1','q2']:
                pool.write(parent/'runs'/q/'result.json',dict(answer=dict(selected_option='1'),error=None))
        return out,control

    def success(self, scheme='P'):
        return dict(complete=True,error=None,answer=dict(selected_option='1'),arms={'O':{}})

    def test_different_workers_claim_distinct_questions(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td))
            self.assertEqual(pool.claim(out,c,0,'P'),(1,'q0'))
            self.assertEqual(pool.claim(out,c,1,'P'),(2,'q1'))

    def test_shared_failure_stops_claims(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.mark_failure(c,ValueError('bad'),0)
            self.assertIsNone(pool.claim(out,c,1,'P'))
            pool.mark_failure(c,RuntimeError('later'),1)
            self.assertEqual(pool.read(c/'failure.json')['message'],'bad')

    def test_source_wait_does_not_claim_or_skip_question(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));parent=Path(pool.read(out/'experiment.json')['parent_N'])
            (parent/'runs/q0/result.json').unlink()
            self.assertEqual(pool.claim(out,c,0,'P'),('wait_source','q0'))
            self.assertFalse(pool.read(c/'claims.json'))

    def test_failed_upstream_is_not_retried(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));parent=Path(pool.read(out/'experiment.json')['parent_N'])
            pool.write(parent/'runs/q0/result.json',dict(error='failure'))
            with self.assertRaisesRegex(ValueError,'Unsuccessful upstream'):
                pool.claim(out,c,0,'O')

    def test_unclaimed_existing_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.write(out/'runs/q0/result.json',self.success())
            with self.assertRaisesRegex(ValueError,'Unclaimed'):
                pool.claim(out,c,0,'P')

    def test_initialize_reuses_only_complete_successes(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.write(out/'runs/q0/result.json',self.success())
            self.assertEqual(pool.initialize_claims(out,c,'P'),1)
            self.assertEqual(pool.claim(out,c,0,'P'),(2,'q1'))
            (out/'runs/q2').mkdir()
            with self.assertRaisesRegex(ValueError,'only successful'):
                pool.initialize_claims(out,c,'P')

    def test_M_shared_cache_is_preserved_outside_question_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td),'M');pool.write(out/'runs/q0/result.json',self.success('M'))
            pool.write(out/'runs/memory_cache/M/archive.json',dict(cached='original'))
            self.assertEqual(pool.completed(out,'M'),['q0'])
            self.assertTrue(pool.boundary_ready(out,'M','q0'))
            self.assertEqual(pool.initialize_claims(out,c,'M'),1)
            self.assertEqual(pool.read(out/'runs/memory_cache/M/archive.json'),dict(cached='original'))

    def test_publication_preserves_absolute_raw_reference(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.claim(out,c,0,'P');attempt=c/'attempts/q0/run'
            pool.write(attempt/'result.json',self.success());pool.write(attempt/'raw_calls/a.json',dict(raw='original'))
            raw=attempt/'raw_calls/a.json';before=raw.read_bytes()
            pool.publish(out,c,'q0',attempt,'P')
            self.assertTrue(attempt.is_symlink());self.assertEqual(raw.read_bytes(),before)
            self.assertEqual(pool.read(c/'claims.json')['q0']['state'],'complete')
            with self.assertRaisesRegex(ValueError,'Duplicate'):
                pool.publish(out,c,'q0',attempt,'P')

    def test_pending_call_prevents_publication(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.claim(out,c,0,'O');attempt=c/'attempts/q0/run'
            pool.write(attempt/'result.json',self.success());pool.write(attempt/'x.pending.json',{})
            with self.assertRaisesRegex(ValueError,'pending'):
                pool.publish(out,c,'q0',attempt,'O')

    def test_failure_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.claim(out,c,0,'P');attempt=c/'attempts/q0/run'
            pool.write(attempt/'result.json',dict(error='bad',complete=False))
            with self.assertRaisesRegex(ValueError,'incomplete'):
                pool.publish(out,c,'q0',attempt,'P')

    def test_boundary_rejects_partial_next_question(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.write(out/'runs/q0/result.json',self.success())
            self.assertTrue(pool.boundary_ready(out,'P','q0'))
            (out/'runs/q1').mkdir()
            self.assertFalse(pool.boundary_ready(out,'P','q0'))

    def test_boundary_rejects_pending_or_partial_source_copy(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.write(out/'runs/q0/result.json',self.success())
            pending=out/'runs/q0/a.pending.json';pool.write(pending,{})
            self.assertFalse(pool.boundary_ready(out,'P','q0'));pending.unlink()
            (out/'source_staging/q1').mkdir(parents=True)
            self.assertFalse(pool.boundary_ready(out,'P','q0'))

    def test_O_view_uses_exact_frozen_inputs_and_separate_runs(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));view=c/'view';pool.prepare_view(out,view)
            self.assertTrue((view/'cases').is_symlink())
            self.assertEqual((view/'experiment.json').resolve(),(out/'experiment.json').resolve())
            self.assertFalse((view/'runs').is_symlink())
            self.assertNotEqual((view/'runs').resolve(),(out/'runs').resolve())

    def test_P_adapter_passes_exact_one_question_to_unchanged_engine(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.claim(out,c,0,'P')
            case=out/'cases/q0';question=dict(question_id='q0',question='Which object?',options=['a','b'])
            sources=[dict(id='S0',text='A source sentence.')];original=dict(prompts=['Exact original'])
            for name,value in [('question.json',question),('sentences.json',sources),('original_qa.json',original)]:pool.write(case/name,value)
            source=types.ModuleType('script.run_o_full_experiment');source.prepare_case=lambda root,q:root==out and q=='q0';source.verify_case=lambda folder:None
            engine=types.ModuleType('script.run_p_sentence_experiment');seen=[]
            cfg=dict(budget=dict(tool_call_cap=150,final_answer_reserve=45))
            def run(backend,actual_cfg,q,ss,raw,target,**extra):
                seen.append((actual_cfg,q,ss,raw,extra));pool.write(target/'result.json',self.success())
            engine.run_case=run
            with patch.dict(sys.modules,{'script.run_o_full_experiment':source,'script.run_p_sentence_experiment':engine}):
                pool.run_question(out,c,dict(scheme='P'),0,'q0',object(),cfg)
            self.assertEqual(seen,[(cfg,question,sources,original,{})])
            self.assertTrue((out/'runs/q0/result.json').exists())

    def test_O_adapter_preserves_full_policy_and_runs_only_claimed_question(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.claim(out,c,0,'O')
            (c/'workers/0').mkdir(parents=True)
            policy=pool.read(out/'experiment.json');policy.update(case_ids=['q0','q1','q2'],case_budget_seconds=3600,conditions=['question_only'])
            pool.write(out/'experiment.json',policy)
            source=types.ModuleType('script.run_o_full_experiment');source.prepare_case=lambda root,q:root==out and q=='q0';source.verify_case=lambda folder:None
            engine=types.ModuleType('script.o_batched_matrix');seen=[]
            def run(view,backend,actual_cfg,*,case_ids):
                seen.append((pool.read(view/'experiment.json'),case_ids))
                pool.write(view/'runs'/case_ids[0]/'result.json',self.success('O'))
            engine.run=run
            with patch.dict(sys.modules,{'script.run_o_full_experiment':source,'script.o_batched_matrix':engine}):
                pool.run_question(out,c,dict(scheme='O'),0,'q0',object(),{})
            self.assertEqual(seen,[(policy,['q0'])])
            self.assertFalse((out/'runs/q1').exists())
            self.assertEqual((c/'workers/0/view/runs/q0').resolve(),(out/'runs/q0').resolve())

    def test_real_process_drain_waits_for_completion_and_closes_idle_worker(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));q=out/'runs/q0';q.mkdir()
            pool.write(out/'progress.json',dict(question_id='q0'))
            result=q/'result.json';closed=out/'closed'
            code=('import time,json;from pathlib import Path\n'
                  'try:\n time.sleep(.3)\n Path('+repr(str(result))+').write_text(json.dumps('+repr(self.success())+'))\n time.sleep(30)\n'
                  'finally:\n Path('+repr(str(closed))+').write_text("closed")\n')
            worker=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            controller=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            cfg=dict(scheme='P',original_worker=dict(pid=worker.pid,identity=pool.identity(worker.pid)),
                     original_controller=dict(pid=controller.pid,identity=pool.identity(controller.pid)))
            pool.write(out/'queue_status.json',dict(state='running'))
            try:
                pool.drain(out,c,cfg)
                worker.wait(timeout=5);controller.wait(timeout=5)
                self.assertTrue(closed.exists())
                self.assertTrue(pool.read(c/'boundary.json')['passed'])
                self.assertEqual(pool.read(c/'boundary.json')['pending_model_calls'],0)
                self.assertTrue(pool.complete_result(pool.read(result),'P'))
            finally:
                for child in [worker,controller]:
                    if child.poll() is None:child.kill()
                    child.wait()


if __name__=='__main__':
    unittest.main()
