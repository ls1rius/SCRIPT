from pathlib import Path
import tempfile
import unittest
import shutil
from types import SimpleNamespace
from unittest.mock import Mock, patch
from script import controlled_memory_io as io
from script.schedule_evidence_evaluations import next_normalize, next_qa, PARENT_ARMS, run


class SchedulerTests(unittest.TestCase):
    def test_normalization_only_finishes_without_launching_an_extra_qa_model(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td)/'parent';control=parent/'control/normalization'
            io.write(parent/'cohort.json',['q0'])
            io.write(parent/'evaluation_only.json',dict(q0='0'))
            io.write(parent/'historical_only.json',dict(q0=dict(selected_option='0')))
            io.write(parent/'experiment.json',{})
            io.write(parent/'frozen_hashes.json',{})
            io.write(parent/'validation/baseline_reproduction.json',dict(passed=True))
            io.write(parent/'materialized/q0/sealed.json',{})
            io.write(parent/'final_inputs/C_semantic/q0.json',{})
            io.write(parent/'handoff.json',dict(parent=str(parent),active=[]))
            (parent/'control').mkdir(exist_ok=True)
            shutil.copy2(io.__file__,parent/'control/controlled_memory_io.py')
            compact=SimpleNamespace(inventory=Mock())
            with patch.dict('sys.modules',run_compact_evidence_experiment=compact):
                run(parent,None,control,parent/'handoff.json',normalization_only=True)
            compact.inventory.assert_not_called()
            self.assertEqual(io.read(control/'status.json')['state'],'normalization_complete_waiting_for_answers')
            self.assertFalse(io.read(parent/'report.json')['complete'])

    def test_normalizers_receive_disjoint_blocks_and_partial_work_is_not_restarted(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);ids=[f'q{i}' for i in range(60)]
            active=[dict(kind='normalize',start=0,end=20)]
            self.assertEqual(next_normalize(p,ids,active),(20,40))
            (p/'materialized/q20').mkdir(parents=True)
            with self.assertRaises(ValueError):next_normalize(p,ids,active)

    def test_qa_reads_only_sealed_inputs_and_reserves_inflight_question_ranges(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);ids=['q0','q1']
            for uid in ids:
                for arm in PARENT_ARMS:
                    f=p/'final_inputs'/arm/f'{uid}.json';f.parent.mkdir(parents=True,exist_ok=True);f.write_text('{}')
            self.assertIsNone(next_qa(p,ids,'parent_qa',[]))
            for uid in ids:
                f=p/'materialized'/uid/'sealed.json';f.parent.mkdir(parents=True);f.write_text('{}')
            self.assertEqual(next_qa(p,ids,'parent_qa',[]),(0,2))
            self.assertEqual(next_qa(p,ids,'parent_qa',[dict(kind='parent_qa',start=0,end=1)]),(1,2))


if __name__=='__main__':unittest.main()
