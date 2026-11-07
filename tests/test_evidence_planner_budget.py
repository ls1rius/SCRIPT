from pathlib import Path
import json
import tempfile
import unittest
from script_core.budget import BudgetLedger, Meter
from script_core.config import load_config


class Backend:
    def __init__(self):self.calls=[]
    def call(self,op,inputs,timeout):
        self.calls.append((op,timeout));return dict(raw='{}')


class PlannerBudgetTests(unittest.TestCase):
    def test_continuation_separates_success_failure_and_new_costs(self):
        from script.n_planner_continuation import ContinuationLedger
        ledger=ContinuationLedger(1500,1096.684983,140.034381,45,None)
        self.assertAlmostEqual(ledger.allowance(1500),218.280636,places=6)
        ledger.actual=180
        result=ledger.summary()
        self.assertAlmostEqual(result['total'],1276.684983)
        self.assertAlmostEqual(result['budgeted_total'],1416.719364)
        self.assertEqual(result['new_processing_seconds'],180)
        self.assertEqual(result['historical_failed_dependency_seconds'],140.034381)
        self.assertEqual(result['shared_dependency_seconds'],0)
        self.assertAlmostEqual(ledger.allowance(1500),38.280636,places=6)

    def run_call(self,op,*,final=False,build=False,task_cap=None):
        cfg=load_config();cfg['budget'].update(tool_call_cap=150,build_call_cap=600,final_answer_reserve=45)
        ledger=BudgetLedger(1500,initialization_cost=1000,answer_reserve=45)
        ledger.dependency_cost=140;ledger.warmup_reserve=10;ledger.cleanup_reserve=5
        ledger.task_cap=task_cap
        backend=Backend()
        with tempfile.TemporaryDirectory() as td:
            ledger.calls_path=Path(td)/'calls.jsonl';meter=Meter(backend,ledger,cfg,Path(td)/'raw_calls')
            try:meter.call(op,{},final=final,build=build)
            finally:meter.close()
            saved=json.loads(ledger.calls_path.read_text().splitlines()[-1])
        return saved,backend,ledger

    def test_all_evidence_planners_use_remaining_budget_with_reserves(self):
        for op in ['l_evidence_plan','m_evidence_plan','n_evidence_plan']:
            row,backend,ledger=self.run_call(op)
            self.assertAlmostEqual(row['reservation'],300,places=2)
            self.assertEqual(row['reservation_policy'],'remaining_nonanswer_budget_preserving_all_reserves')
            self.assertLess(backend.calls[0][1],row['reservation'])
            self.assertEqual(ledger.dependency_cost,140)
            self.assertFalse(ledger.budget_violation)

    def test_task_local_cap_is_preserved(self):
        row,_,_=self.run_call('n_evidence_plan',task_cap=70)
        self.assertAlmostEqual(row['reservation'],70,places=2)

    def test_unrelated_tools_keep_original_per_call_cap(self):
        row,_,_=self.run_call('n_extract_batch')
        self.assertEqual(row['reservation'],150)
        self.assertEqual(row['reservation_policy'],'configured_per_call_cap')

    def test_final_or_build_flags_do_not_bypass_original_limits(self):
        row,_,_=self.run_call('n_evidence_plan',final=True)
        self.assertEqual(row['reservation'],45)
        self.assertEqual(row['reservation_policy'],'configured_per_call_cap')
        row,_,_=self.run_call('n_evidence_plan',build=True)
        self.assertEqual(row['reservation'],300)
        self.assertEqual(row['reservation_policy'],'configured_per_call_cap')


if __name__=='__main__':unittest.main()
