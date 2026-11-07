import json
from pathlib import Path
import shutil
import tempfile
import unittest

from script import resume_q4_oom as r


ROOT=Path(__file__).resolve().parents[1]/'workdirs/Q4-adaptive-500-20261105-r1'
CONTROL=ROOT/'recovery-20261106-r3'


@unittest.skipUnless((CONTROL/'oom_state.json').exists(),'requires saved OOM checkpoint')
class OOMResumeTests(unittest.TestCase):
    def test_exact_failed_request_is_first_new_call_and_successful_prefix_is_not_replayed(self):
        from script_core.budget import ToolFailure
        state=r.read(CONTROL/'oom_state.json');uid=state['uid'];seen=[]
        class StopBackend:
            def call(self,operation,inputs,*args):
                seen.append((operation,inputs));raise ToolFailure('CPU boundary probe')
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);source=out/'source_attempts'/uid/'run'
            shutil.copytree(CONTROL/'failed_source_snapshot',source)
            (out/'source_inputs').mkdir();shutil.copy2(ROOT/'source_inputs'/f'{uid}.json',out/'source_inputs'/f'{uid}.json')
            target=source/'partial_search';prefix=(target/'calls.jsonl').read_bytes()
            with self.assertRaisesRegex(RuntimeError,'CPU boundary probe'):
                r.resume_search(out,CONTROL,StopBackend(),r.read(ROOT/'source_config.json'),target)
            self.assertEqual(len(seen),1);self.assertEqual(seen[0][0],state['failed']['operation'])
            from script_core.config import digest
            self.assertEqual(digest(seen[0][1]),state['failed']['input_hash'])
            self.assertTrue((target/'calls.jsonl').read_bytes().startswith(prefix))
            check=r.read(target/'oom_first_call.json');self.assertTrue(check['passed'])
            # Small CPU work between restoring the ledger and the model call is
            # newly metered in addition to the intact historical prefix.
            self.assertGreaterEqual(check['historical_seconds'],state['result']['cost_summary']['online'])
            self.assertLess(check['historical_seconds']-state['result']['cost_summary']['online'],.01)
            calls=r.read_calls(target/'calls.jsonl');self.assertEqual(calls[:len(state['calls'])],state['calls'])
            result=r.read(target/'result.json')
            self.assertAlmostEqual(result['cost_summary']['online'],sum(c['actual_cost'] for c in calls))
            self.assertEqual(result['cost_summary']['total_cap'],1500)

    def test_pending_claims_remain_schedulable_when_all_cohort_ids_are_present(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);r.write(out/'cohort.json',['a','b']);r.write(out/'claims.json',{'a':{'state':'complete'},'b':{'state':'running','recovery_pending':True}})
            self.assertEqual(r.pending_remaining(out),1)


if __name__=='__main__':unittest.main()
