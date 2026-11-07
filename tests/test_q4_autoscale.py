import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script import autoscale_q4_500 as s
from script import extend_q4_500 as ext


class AutoscaleTests(unittest.TestCase):
    def config(self):
        return dict(maximum_workers=8,new_gpu_indices=['2','3'],maximum_new_workers_per_gpu=2,
            launch_minimum_effective_free_mib=32768,launch_stable_observations=3)

    def test_adaptation_changes_only_explicit_scheduler_and_idle_drain_lines(self):
        root=Path(__file__).resolve().parents[1]/'workdirs/Q4-adaptive-500-20261105-r1'
        original=(root/'runtime/script/extend_q4_500.py').read_text()
        start=original.index('def worker(out,wid):');end=original.index('\ndef supervise(out):',start)
        original=original[start:end].rstrip()+'\n';changed,edits=s.generated_worker(original)
        compile(changed,'new-worker','exec')
        for before,after in reversed(edits):
            self.assertEqual(changed.count(after),1)
            changed=changed.replace(after,before,1)
        self.assertEqual(changed,original)
        self.assertEqual(len(edits),4)

    def test_drain_never_discards_owned_inflight_case(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);s.write(p/'cohort.json',['a','b']);s.write(p/'claims.json',{'a':{'state':'running'},'b':{'state':'complete'}})
            self.assertFalse(s.can_drain(p,{'question_id':'a'}))
            self.assertTrue(s.can_drain(p,{'question_id':'b'}))
            self.assertTrue(s.can_drain(p,{}))
            s.write(p/'claims.json',{'a':{'state':'complete'}})
            self.assertFalse(s.can_drain(p,{}));self.assertEqual(s.work_remaining(p),1)

    def test_no_capacity_never_adds_worker(self):
        cfg=self.config();stable={}
        for _ in range(5):
            self.assertIsNone(s.launch_candidate(cfg,[],{}, {},{'2':12201,'3':12867},{},stable,100))
        self.assertEqual(stable,{'2':0,'3':0})

    def test_capacity_must_be_stable_and_launch_resets_counter(self):
        cfg=self.config();stable={};free={'2':50000,'3':12000}
        self.assertIsNone(s.launch_candidate(cfg,[],{}, {},free,{},stable,100))
        self.assertIsNone(s.launch_candidate(cfg,[],{}, {},free,{},stable,100))
        self.assertEqual(s.launch_candidate(cfg,[],{}, {},free,{},stable,100),'2')
        self.assertEqual(stable['2'],0)

    def test_pending_worker_capacity_is_reserved_before_its_lease_appears(self):
        cfg=self.config();stable={};rows=[dict(id=4,pid=100,assigned_gpu='2',adopted=False)]
        for _ in range(4):
            self.assertIsNone(s.launch_candidate(cfg,rows,{}, {},{'2':50000,'3':12000},{},stable,100))
        stable={};leases={'lease':{'pid':100}}
        for _ in range(2):self.assertIsNone(s.launch_candidate(cfg,rows,{}, {},{'2':50000,'3':12000},leases,stable,100))
        self.assertEqual(s.launch_candidate(cfg,rows,{}, {},{'2':50000,'3':12000},leases,stable,100),'2')

    def test_worker_limits_no_tasks_and_failure_block_launch(self):
        cfg=self.config();free={'2':90000,'3':90000}
        rows=[dict(id=i,pid=100+i,adopted=i<4,assigned_gpu='2' if i<6 else '3') for i in range(8)]
        self.assertIsNone(s.launch_candidate(cfg,rows,{}, {},free,{}, {},100))
        self.assertIsNone(s.launch_candidate(cfg,[],{}, {},free,{}, {},0))
        self.assertIsNone(s.launch_candidate(cfg,[],{}, {},free,{}, {},100,failed=True))
        rows=rows[:6];stable={'2':10,'3':10}
        self.assertEqual(s.launch_candidate(cfg,rows,{}, {},free,{},stable,100),'3')

    def test_handoff_signals_only_exact_supervisor_and_preserves_worker_identities(self):
        live={10:'old',11:'worker',12:'model'};signals=[]
        def send(pid,sig):signals.append((pid,sig));live.pop(pid)
        s.handoff_supervisor(dict(pid=10,identity='old'),[dict(pid=11,identity='worker')],
            process_identity=lambda pid:live.get(pid),send_signal=send)
        self.assertEqual([pid for pid,sig in signals],[10])
        self.assertEqual(live,{11:'worker',12:'model'})
        with self.assertRaises(ValueError):
            s.handoff_supervisor(dict(pid=10,identity='old'),[],process_identity=lambda pid:'reused',send_signal=send)

    def test_dead_worker_with_missing_identity_is_not_alive(self):
        with patch.object(s,'identity',return_value=None):
            self.assertFalse(s.alive(dict(pid=123,identity=None)))
            self.assertFalse(s.alive(dict(pid=123,identity='original')))


if __name__=='__main__':unittest.main()
