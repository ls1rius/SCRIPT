"""Saved-checkpoint recovery must conserve source calls and paid processing."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from script import resume_q4_failures as recovery
from script_core.config import digest
from script_core.temporal_pipeline import qa_context_inputs
from script_core.adaptive_transition import memory_context


ROOT=Path(__file__).resolve().parents[1]/'workdirs/Q4-adaptive-500-20261105-r1'
UID='55ea09ed-4590-4e59-8753-40a64d67abd9'
SAVED=ROOT/'recovery-20261106-r2/failed_source_snapshot'


@unittest.skipUnless(SAVED.exists(),'requires archived optional-plan failure fixture')
class SourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.out=Path(self.temp.name)/'out';self.control=Path(self.temp.name)/'control';self.control.mkdir()
        self.source=self.out/'source_attempts'/UID/'run'
        shutil.copytree(SAVED,self.source)
        (self.out/'source_inputs').mkdir()
        shutil.copy2(ROOT/'source_inputs'/f'{UID}.json',self.out/'source_inputs'/f'{UID}.json')
        shutil.copy2(ROOT/'source_config.json',self.out/'source_config.json')

    def test_completed_prefix_is_not_called_again_and_cost_is_retained_once(self):
        target=self.source/'partial_search';before=recovery.tree(self.source)
        prefix=(target/'calls.jsonl').read_bytes();old=recovery.read(target/'result.json')
        recovery.recover_optional_plan(self.out,self.control,UID)
        result=recovery.read(target/'result.json');bundle=recovery.read(target/'evidence_bundle.json')
        self.assertIsNone(result['error']);self.assertTrue(result['evidence_only'])
        self.assertEqual(result['recovery']['model_calls_repeated'],0)
        self.assertTrue((target/'calls.jsonl').read_bytes().startswith(prefix))
        calls=[json.loads(l) for l in (target/'calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(calls),old['cost_summary']['call_count']+1)
        self.assertTrue(calls[-1]['parameters']['in_process'])
        self.assertAlmostEqual(result['cost_summary']['online'],old['cost_summary']['online']+calls[-1]['actual_cost'])
        self.assertFalse(result['cost_summary']['budget_violation'])
        changed={rel for rel,h in before.items() if recovery.sha(self.source/rel)!=h}
        self.assertEqual(changed,{'partial_search/result.json','partial_search/calls.jsonl','partial_search/sparse_visual.json'})
        self.assertEqual(bundle['supplemental_visual']['observations'],[])
        packet=recovery.read(self.out/'source_inputs'/f'{UID}.json')
        self.assertEqual(bundle['packet_hash'],digest(packet))
        expected=qa_context_inputs(dict(packet['question'],id=UID),packet['captions'],bundle['selected_seconds'],
                                   memory_context(bundle['memory']),visual_observations=[])
        self.assertEqual(bundle['qa_input'],expected)

    def test_other_failures_and_pending_calls_cannot_use_this_continuation(self):
        path=self.source/'partial_search/result.json';result=recovery.read(path)
        result['error']['message']='Invalid unrelated operation JSON: bad'
        recovery.write(path,result)
        with self.assertRaisesRegex(ValueError,'exact optional-plan failure'):
            recovery.recover_optional_plan(self.out,self.control,UID)
        shutil.copy2(SAVED/'partial_search/result.json',path)
        recovery.write(self.source/'active.pending.json',{})
        with self.assertRaisesRegex(ValueError,'Unexpected work'):
            recovery.recover_optional_plan(self.out,self.control,UID)


if __name__=='__main__':unittest.main()
