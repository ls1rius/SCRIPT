from pathlib import Path
import signal
import tempfile
import unittest
from contextlib import contextmanager
import fcntl
from types import SimpleNamespace
from unittest.mock import patch

from script import scale_live_parallel_pool as scale


class LiveScaleTests(unittest.TestCase):
    def test_handoff_signals_only_supervisor(self):
        processes = {10: 'old', 20: 'a', 30: 'b'}
        signals = []
        def send(pid, sig):
            signals.append((pid, sig))
            processes.pop(pid)
        scale.handoff_supervisor(dict(pid=10, identity='old'),
            [dict(pid=20, identity='a'), dict(pid=30, identity='b')],
            process_identity=processes.get, send_signal=send)
        self.assertEqual(signals, [(10, signal.SIGTERM)])
        self.assertEqual(processes, {20:'a', 30:'b'})

    def test_pid_reuse_prevents_supervisor_signal(self):
        calls = []
        with self.assertRaisesRegex(ValueError, 'controller identity'):
            scale.handoff_supervisor(dict(pid=10, identity='old'), [],
                process_identity=lambda pid:'reused', send_signal=lambda *a:calls.append(a))
        self.assertFalse(calls)

    def test_missing_worker_prevents_handoff(self):
        calls = []
        with self.assertRaisesRegex(ValueError, 'worker identity'):
            scale.handoff_supervisor(dict(pid=10, identity='old'), [dict(pid=20,identity='worker')],
                process_identity={10:'old'}.get, send_signal=lambda *a:calls.append(a))
        self.assertFalse(calls)

    def test_dead_inflight_worker_is_failure(self):
        self.assertIn('without complete', scale.worker_health(dict(id=2,pid=20,identity='x'),
            dict(stage='query'), lambda pid:None))

    def test_successfully_finished_worker_is_not_failure(self):
        self.assertIsNone(scale.worker_health(dict(id=2,pid=20,identity='x'),
            dict(stage='complete'), lambda pid:None))

    def test_alive_failed_worker_still_reports_failure(self):
        self.assertEqual(scale.worker_health(dict(id=2,pid=20,identity='x'),
            dict(stage='failed',error='bad'), lambda pid:'x'), 'bad')

    def test_reused_worker_pid_is_not_alive(self):
        self.assertIn('without complete',scale.worker_health(dict(id=2,pid=20,identity='x'),
            dict(stage='query'),lambda pid:'different'))

    def test_queue_includes_adopted_and_added_workers(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            for wid in range(4):
                scale.write(p/'workers'/str(wid)/'status.json',dict(stage='query',pid=100+wid,index=wid+1))
            rows=[dict(id=i,pid=100+i,identity='x',adopted=i<2) for i in range(4)]
            q=scale.queue_snapshot(p,p,rows)
            self.assertEqual(q['worker_pids'],[100,101,102,103])
            self.assertEqual(set(q['workers']),{'0','1','2','3'})
            self.assertEqual(q['effective_worker_count'],4)

    def test_frozen_manifest_detects_changes(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);f=p/'model_config.json';scale.write(f,dict(batch_size=4,budget=1500))
            scale.write(p/'frozen_hashes.json',{f.name:scale.sha(f)})
            scale.verify_manifest(p)
            scale.write(f,dict(batch_size=8,budget=1500))
            with self.assertRaisesRegex(ValueError,'Frozen artifact changed'):
                scale.verify_manifest(p)

    def test_failed_pool_cannot_be_scaled_or_retried(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);c=p/'control';target=p/'new'
            scale.write(p/'queue_status.json',dict(state='running',scheduler=str(c)))
            scale.write(c/'failure.json',dict(message='old failure'))
            with self.assertRaisesRegex(ValueError,'must not retry'):
                scale.prepare(p,c,target,2)
            self.assertFalse(target.exists())

    def test_generic_pool_keeps_its_frozen_worker_policy(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);policy=dict(scheme='P',workers=2,resource_policy=dict(minimum_free_mib=32768))
            scale.write(p/'config.json',policy)
            self.assertEqual(scale.pool_policy(p),('generic',policy))
            self.assertEqual(scale.read(p/'config.json')['workers'],2)

    def test_generic_worker_command_uses_exact_existing_entry(self):
        c=Path('/existing/P/parallel');out=c.parent
        command=scale.worker_command(c,out,5,'generic','python')
        self.assertEqual(command,['python','-u',str(c/'runner.py'),'worker','--control',str(c),'--worker-id','5'])

    def test_original_MN_entry_remains_available(self):
        c=Path('/existing/N/parallel');out=c.parent
        command=scale.worker_command(c,out,2,'mn','python')
        self.assertIn(str(c/'runtime/script/run_parallel_scene_scheduler.py'),command)
        self.assertIn('--out',command)

    def test_generic_report_does_not_recursively_lock_claims(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);c=out/'parallel';c.mkdir()
            scale.write(c/'config.json',dict(scheme='P',workers=2))
            @contextmanager
            def locking(control):
                with (control/'claims.lock').open('a') as f:
                    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    yield
            def report(root,control,cfg):
                with locking(control):
                    scale.write(root/'report.json',dict(completed=114,scheduler_failure=None,parallel_worker_count=cfg['workers']))
            module=SimpleNamespace(report=report,locked=locking)
            result=scale.refresh_scaled_report(out,c,module,dict(pool_kind='generic',scale_path='/scale',frozen_policy_workers=2),6)
            self.assertEqual(result['parallel_worker_count'],6)
            self.assertEqual(result['completed'],114)
            self.assertEqual(scale.read(c/'config.json')['workers'],2)

    def test_generic_monitor_and_source_entry_share_live_queue(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);c=out/'parallel';snapshot=dict(state='running',controller_pid=123,effective_worker_count=6)
            scale.write_queue(out,c,snapshot,'generic')
            self.assertEqual(scale.read(out/'queue_status.json'),snapshot)
            self.assertEqual(scale.read(c/'queue_status.json'),snapshot)
            self.assertEqual(scale.read(c/'status.json'),snapshot)


if __name__ == '__main__':
    unittest.main()
