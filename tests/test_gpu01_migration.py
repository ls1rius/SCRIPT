from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from script import gpu01_migration as migration


class GPUWithdrawalTests(unittest.TestCase):
    def test_truncated_journal_keeps_only_durable_complete_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'calls.jsonl';p.write_text('{"actual_cost":3}\n{"partial":')
            self.assertEqual(migration.journal_prefix(p),[dict(actual_cost=3)])

    def test_checkpoint_conservatively_charges_pending_and_does_not_change_raw(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);out=root/'P';c=out/'parallel';target=root/'migration'
            child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            try:
                row=dict(id=2,pid=child.pid,identity=migration.identity(child.pid),gpu='3',scheme='P')
                migration.write(c/'claims.json',dict(q=dict(state='running',index=2,pid=child.pid)))
                r=c/'attempts/q/run';migration.write(r/'raw_calls/a.pending.json',dict(reservation=150,operation='p_independent_batch'))
                f=r/'calls.jsonl';f.parent.mkdir(parents=True,exist_ok=True);f.write_text('{"actual_cost":4,"error":null}\n')
                before=f.read_bytes();info=migration.stop_and_checkpoint(out,c,row,target)
                child.wait(timeout=5)
                self.assertEqual(info['successful_seconds'],4)
                self.assertEqual(info['interrupted_budget_seconds'],150)
                self.assertEqual(f.read_bytes(),before)
                self.assertEqual(migration.read(c/'claims.json')['q']['state'],'migration_paused')
                self.assertFalse((c/'failure.json').exists())
                self.assertTrue((r/'raw_calls/a.pending.json').exists())
            finally:
                if child.poll() is None:child.kill()
                child.wait()

    def test_reused_pid_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with self.assertRaisesRegex(ValueError,'PID changed'):
                migration.stop_and_checkpoint(root,root,dict(pid=1,identity='incorrect'),root)


if __name__=='__main__':unittest.main()
