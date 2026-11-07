import json
from pathlib import Path
import tempfile
import unittest

from script import controlled_memory_io as io
from script import run_n_scene_experiment as runner
from script_core.n_scene_memory import VERSION


class NExperimentTests(unittest.TestCase):
    def fixture(self, root, empty=False):
        io.write(root/'partial_temporal/result.json',dict(memory_variant='N',method=VERSION,
            qa_evidence=dict(database=str(root/'memory_cache/N/I/h_script.sqlite3'),
                extractor_version='H:N-required-role-review-v2')))
        for name in ('final_input.json','proposed_plan.json','plan.json','source_preservation.json','audit.json'):
            io.write(root/'partial_temporal/N'/name,{})
        ops=['n_answer'] if empty else ['n_extract_batch','n_evidence_plan','n_answer']
        (root/'partial_temporal/calls.jsonl').write_text('\n'.join(json.dumps(dict(operation=op)) for op in ops))

    def test_variant_contract_rejects_wrong_cache_and_m_calls(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.fixture(root)
            self.assertTrue(runner.audit_variant(root)['passed'])
            path=root/'partial_temporal/result.json';r=io.read(path)
            r['qa_evidence']['database']=str(root/'memory_cache/M/I/h_script.sqlite3');io.write(path,r)
            with self.assertRaisesRegex(ValueError,'cache namespace'):runner.audit_variant(root)
            self.fixture(root)
            with (root/'partial_temporal/calls.jsonl').open('a') as f:
                f.write('\n'+json.dumps(dict(operation='m_evidence_plan')))
            with self.assertRaisesRegex(ValueError,'isolated'):runner.audit_variant(root)

    def test_empty_path_skips_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.fixture(root,empty=True)
            self.assertTrue(runner.audit_variant(root,empty=True)['passed'])
            with (root/'partial_temporal/calls.jsonl').open('a') as f:
                f.write('\n'+json.dumps(dict(operation='n_extract_batch')))
            with self.assertRaisesRegex(ValueError,'Empty N'):runner.audit_variant(root,empty=True)

    def test_report_counts_n_final_answer_tokens_and_all_call_costs(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            io.write(root/'cohort.json',['q'])
            io.write(root/'evaluation_only.json',dict(q=2))
            io.write(root/'experiment.json',dict(experiment_label='N'))
            io.write(root/'runs/q/result.json',dict(answer=dict(selected_option='2'),error=None))
            io.write(root/'runs/q/partial_search/result.json',dict(opened_scenes=['S1']))
            p=root/'runs/q/partial_temporal/calls.jsonl';p.parent.mkdir(parents=True)
            p.write_text('\n'.join(json.dumps(dict(operation=op,actual_cost=cost,input_tokens=tokens))
                for op,cost,tokens in [('n_evidence_plan',10,500),('n_answer',.5,123)]))
            report=runner.common.report(root)
            self.assertEqual(report['mean_input_tokens'],123)
            self.assertEqual(report['mean_qa_seconds'],.5)
            self.assertEqual(report['measured_query_seconds'],10.5)
            self.assertEqual(report['correct'],1)


if __name__=='__main__':unittest.main()
