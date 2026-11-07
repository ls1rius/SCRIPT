from pathlib import Path
import json
import tempfile
import unittest
from script.gpu01_recovery_worker import PrefixReplay,replay_meters
from script.scale_live_parallel_pool import read,write
from script_core.budget import BudgetLedger,Meter
from script_core.config import digest


class Backend:
    def __init__(self):self.calls=[]
    def call(self,operation,inputs,timeout):
        self.calls.append((operation,inputs))
        return dict(data=dict(value=inputs['value']),usage=dict(input_tokens=3,output_tokens=1))


CFG=dict(models=dict(device='cpu'),query=dict(tool_transient_retries=0),
         budget=dict(tool_call_cap=150,build_call_cap=600,final_answer_reserve=45),cache={})


class ReplayTests(unittest.TestCase):
    def saved(self,root,region='.'):
        folder=root/region;ledger=BudgetLedger(3600,answer_reserve=45,calls_path=folder/'calls.jsonl')
        meter=Meter(Backend(),ledger,CFG,folder/'raw_calls')
        output,call=meter.call('o_independent_batch',dict(value='exact',scope=dict(question_id='q')))
        meter.close();return call,output

    def test_exact_replay_avoids_model_call_and_retains_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old=root/'old';call,expected=self.saved(old);target=root/'new'
            cp=dict(original_attempt=str(old),regions={'.':[call]},pending=[dict(call_id='cut',path=str(old/'raw_calls/cut.pending.json'),reservation=150)])
            backend=Backend()
            with replay_meters(cp,target,'P',CFG):
                ledger=BudgetLedger(3600,answer_reserve=45,calls_path=target/'calls.jsonl');meter=Meter(backend,ledger,CFG,target/'raw_calls')
                output,_=meter.call(call['operation'],dict(value='exact',scope=dict(question_id='q')))
                meter.call('o_independent_batch',dict(value='new'))
                summary=ledger.summary();meter.close()
            self.assertEqual(output['data'],expected['data']);self.assertEqual(len(backend.calls),1)
            self.assertAlmostEqual(summary['shared_dependency_seconds'],150+call['actual_cost'])
            self.assertAlmostEqual(summary['budgeted_total'],summary['total']+summary['shared_dependency_seconds'])
            self.assertEqual(summary['total_cap'],3600)

    def test_changed_input_is_rejected_without_regenerating(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);call,_=self.saved(root/'old');backend=Backend()
            cp=dict(original_attempt=str(root/'old'),regions={'.':[call]},pending=[])
            with self.assertRaisesRegex(ValueError,'prefix differs'):
                with replay_meters(cp,root/'new','P',CFG):
                    ledger=BudgetLedger(3600,calls_path=root/'new/calls.jsonl');meter=Meter(backend,ledger,CFG,root/'new/raw_calls')
                    try:meter.call(call['operation'],dict(value='foreign'))
                    finally:meter.close()
            self.assertEqual(backend.calls,[])

    def test_O_stage_total_includes_old_extraction_once(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old=root/'old';call,_=self.saved(old,'shared_extraction');target=root/'new'
            cp=dict(original_attempt=str(old),regions={'shared_extraction':[call]},pending=[])
            with replay_meters(cp,target,'O',CFG):
                ledger=BudgetLedger(3600,calls_path=target/'shared_extraction/calls.jsonl');meter=Meter(Backend(),ledger,CFG,target/'shared_extraction/raw_calls')
                meter.call(call['operation'],dict(value='exact',scope=dict(question_id='q')))
                summary=ledger.summary();meter.close()
            self.assertAlmostEqual(summary['shared_dependency_seconds'],0)
            self.assertAlmostEqual(summary['total'],ledger.actual+call['actual_cost'])
            self.assertAlmostEqual(summary['budgeted_total'],summary['total'])

    def test_committed_pending_marker_is_not_charged_as_interruption_again(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);old=root/'old';call,_=self.saved(old);target=root/'new'
            cp=dict(original_attempt=str(old),regions={'.':[call]},pending=[dict(call_id=call['call_id'],path=str(old/'raw_calls/a.pending.json'),reservation=150)])
            with replay_meters(cp,target,'P',CFG):
                ledger=BudgetLedger(3600,calls_path=target/'calls.jsonl');meter=Meter(Backend(),ledger,CFG,target/'raw_calls')
                meter.call(call['operation'],dict(value='exact',scope=dict(question_id='q')));meter.close()
            self.assertAlmostEqual(ledger.dependency_cost,call['actual_cost'])

    def test_raw_output_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);call,_=self.saved(root);raw=read(call['output_ref']);raw['output']['data']['value']='changed';write(call['output_ref'],raw)
            cache=PrefixReplay(None,[call],digest(CFG['models']))
            with self.assertRaisesRegex(ValueError,'integrity mismatch'):
                cache.get(call['operation'],dict(value='exact',scope=dict(question_id='q')))


if __name__=='__main__':unittest.main()
