import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script.migrate_controlled_gpu import ARMS,live,next_stage


class MigrationTests(unittest.TestCase):
    def fixture(self,p):
        ids=[f'q{i}' for i in range(100)];(p/'cohort.json').write_text(json.dumps(ids))
        for uid in ids[:60]:
            for arm in ARMS:
                f=p/'runs'/arm/uid/'result.json';f.parent.mkdir(parents=True);f.write_text('{}')
        return ids

    def test_inflight_block_is_excluded_from_target_gpu(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);self.fixture(p);(p/'materialized/q61').mkdir(parents=True)
            with patch('script.migrate_controlled_gpu.live',return_value=True):
                stage=next_stage(p,dict(pid=1,start_ticks='1',start=60,end=80))
            self.assertEqual(stage,('normalize',80,100))

    def test_finished_drain_gets_qa_without_repeating_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);ids=self.fixture(p)
            for uid in ids[60:80]:
                f=p/'final_inputs/C_semantic'/f'{uid}.json';f.parent.mkdir(parents=True,exist_ok=True);f.write_text('{}')
            with patch('script.migrate_controlled_gpu.live',return_value=False):
                self.assertEqual(next_stage(p,dict(pid=1,start_ticks='1',start=60,end=80)),('qa',60,80))

    def test_unfinished_spent_extraction_is_never_silently_restarted(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);self.fixture(p);(p/'materialized/q61').mkdir(parents=True)
            with self.assertRaises(ValueError):next_stage(p)

    def test_pid_reuse_guard_checks_process_start_time(self):
        self.assertTrue(live(os.getpid()))
        self.assertFalse(live(os.getpid(),'wrong-start-time'))


if __name__=='__main__':unittest.main()
