from pathlib import Path
import multiprocessing as mp
import tempfile
import unittest
from unittest.mock import patch
from script import controlled_memory_io as io
from script import run_parallel_scene_scheduler as pool


def take_claims(out,control,worker,barrier):
    pool.claim(out,control,worker,dict(index=str(worker)))
    barrier.wait(timeout=10)
    while pool.claim(out,control,worker,dict(index=str(worker))) is not None:pass


class ParallelTests(unittest.TestCase):
    def test_origin_aliases_skip_old_failed_attempt_published_by_later_control(self):
        from script.preserve_parallel_call_paths import preserve
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);out=root/'N';control=root/'old'
            io.write(control/'claims.json',dict(q=dict(state='running'),done=dict(state='complete')))
            io.write(out/'runs/q/result.json',dict(answer='new success'))
            io.write(control/'attempts/q/run/result.json',dict(error='old failure'))
            io.write(out/'runs/done/result.json',dict(answer='old success'))
            self.assertEqual(preserve(out,control),['done'])
            self.assertFalse((control/'attempts/q/run').is_symlink())
            self.assertEqual(io.read(control/'attempts/q/run/result.json'),dict(error='old failure'))

    def test_only_declared_batch_size_override_can_change_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);out=root/'N';control=root/'control'
            original=dict(query=dict(field_extraction_batch_size=2),budget=dict(total=1500))
            io.write(out/'qa_config.json',original)
            self.assertEqual(pool.worker_config(out,control),original)
            new=dict(query=dict(field_extraction_batch_size=4),budget=dict(total=1500))
            io.write(control/'qa_config.json',new);io.write(control/'policy.json',dict(field_extraction_batch_size=4))
            self.assertEqual(pool.worker_config(out,control),new)
            new['budget']['total']=1800;io.write(control/'qa_config.json',new)
            with self.assertRaisesRegex(ValueError,'exceed'):pool.worker_config(out,control)

    def setup_pool(self,root):
        out=root/'N';control=out/'parallel';control.mkdir(parents=True);(out/'runs').mkdir()
        io.write(out/'cohort.json',[f'q{i}' for i in range(20)])
        io.write(control/'claims.json',{})
        return out,control

    def test_two_processes_claim_every_question_once(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));barrier=mp.Barrier(2)
            children=[mp.Process(target=take_claims,args=(out,c,i,barrier)) for i in range(2)]
            for p in children:p.start()
            for p in children:p.join(15);self.assertEqual(p.exitcode,0)
            state=io.read(c/'claims.json')
            self.assertEqual(set(state),set(io.read(out/'cohort.json')))
            self.assertEqual({r['worker_id'] for r in state.values()},{0,1})

    def test_failure_stops_new_claims(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));io.write(c/'failure.json',dict(error='bad'))
            self.assertIsNone(pool.claim(out,c,0,dict(index='0')))
            self.assertEqual(io.read(c/'claims.json'),{})

    def test_declared_inherited_skipped_unclaimed_output_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));io.write(c/'claims.json',dict(q0=dict(state='inherited')))
            self.assertEqual(pool.claim(out,c,0,dict(index='0')),(2,'q1'))
            (out/'runs/q2').mkdir()
            with self.assertRaisesRegex(ValueError,'Unclaimed'):pool.claim(out,c,0,dict(index='0'))

    def test_publication_only_after_complete_no_pending_no_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.claim(out,c,0,dict(index='0'))
            attempt=c/'attempts/q0/run'
            io.write(attempt/'result.json',dict(answer=dict(selected_option='1'),error=None))
            io.write(attempt/'call.pending.json',{})
            with self.assertRaisesRegex(ValueError,'pending'):pool.publish(out,c,'q0',attempt)
            self.assertFalse((out/'runs/q0').exists())
            (attempt/'call.pending.json').unlink()
            with patch.object(pool,'refresh_report') as report:pool.publish(out,c,'q0',attempt)
            self.assertFalse(attempt.exists());self.assertTrue((out/'runs/q0/result.json').exists())
            self.assertEqual(io.read(c/'claims.json')['q0']['state'],'complete');report.assert_called_once()
            from script.preserve_parallel_call_paths import preserve
            self.assertEqual(preserve(out,c),['q0'])
            self.assertEqual(attempt.resolve(),(out/'runs/q0').resolve())
            self.assertEqual(preserve(out,c),[])
            attempt.unlink()
            io.write(attempt/'result.json',dict(answer=dict(selected_option='1'),error=None))
            with self.assertRaisesRegex(ValueError,'Duplicate'):pool.publish(out,c,'q0',attempt)

    def test_failed_output_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as td:
            out,c=self.setup_pool(Path(td));pool.claim(out,c,0,dict(index='0'))
            attempt=c/'attempts/q0/run';io.write(attempt/'result.json',dict(answer=None,error='bad'))
            with self.assertRaisesRegex(ValueError,'failed'):pool.publish(out,c,'q0',attempt)
            self.assertFalse((out/'runs/q0').exists())


if __name__=='__main__':unittest.main()
