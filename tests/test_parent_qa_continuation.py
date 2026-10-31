from pathlib import Path
import tempfile
import unittest
from script import controlled_memory_io as io
from script.run_parent_qa_after_shared import next_range


class ParentContinuationTests(unittest.TestCase):
    def test_recorded_failure_is_not_retried_and_only_unspent_answers_are_selected(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            io.write(out/'cohort.json',['q0','q1'])
            for uid in ['q0','q1']:
                for arm in io.ARMS:
                    io.write(out/'final_inputs'/arm/f'{uid}.json',{})
            for arm in io.ARMS:
                io.write(out/'runs'/arm/'q0/result.json',dict(error=dict(message='retained OOM'),selected_option=None))
            io.write(out/'runs/A_original/q1/result.json',dict(error=None,selected_option='0'))
            self.assertEqual(next_range(out,io),(1,2))
            for arm in io.ARMS:
                io.write(out/'runs'/arm/'q1/result.json',dict(error=None,selected_option='0'))
            self.assertIsNone(next_range(out,io))


if __name__=='__main__':unittest.main()
