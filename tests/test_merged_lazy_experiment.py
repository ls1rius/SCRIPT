from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script import run_merged_lazy_experiment as run
from script_core import merged_lazy_memory as core
from tests.test_merged_lazy_memory import row


ROOT=Path(__file__).resolve().parents[1]


def parent_fixture(path):
    payload=dict(observations=[row(0,'C mixes.'),row(1,'C washes.'),row(2,'C mixes.')])
    text,_=core.parent_description(payload)
    inputs=dict(query_id='q0',head='original system',prompts=[
        '  Description: '+text+'\n\n###\n\n Questions: Same question/options and old memory'])
    folder=path/'materialized/q0'
    for name,value in {
        'experiment.json':dict(arm=run.PARENT_ARM), 'cohort.json':['q0'],
        'evaluation_only.json':dict(q0=0),'historical_only.json':{},
        'qa_config.json':{},'qa_expected_artifacts.json':{},
        'validation/functional.json':dict(passed=True),
        'packets/q0.json':dict(packet=dict(question=dict(question_id='q0',options=['A','B','C','D','E']))),
        'final_inputs/A_original/q0.json':inputs,
        'final_inputs/H_generalized_lazy/q0.json':inputs,
        'materialized/q0/memory.json':payload,
        'materialized/q0/audit.json':dict(retained_original_rows=3),
        'materialized/q0/plan.json':dict(fields=['actions.base']),
        'report.json':dict(measured_seconds_by_stage=dict(materialized=12.5),totals=dict(planning_model_calls=1))
    }.items():io.write(path/name,value)
    io.write(folder/'sealed.json',{name:io.sha(folder/name) for name in ['memory.json','audit.json','plan.json']})
    io.write(path/'runs/H_generalized_lazy/q0/result.json',dict(selected_option='1',error=None,seconds=1,
        input_tokens=100,input_sha256=io.sha(path/'final_inputs/H_generalized_lazy/q0.json')))
    (path/'runtime/qa/script_core').mkdir(parents=True)
    (path/'runtime/qa/script_core/backend.py').write_text('# immutable original QA runtime\n')
    (path/'control').mkdir()
    for name in ['compact_protocol.py','controlled_memory_io.py','qa_evidence_view.py']:
        source=ROOT/('script_core' if name=='qa_evidence_view.py' else 'script')/name
        if source.exists():(path/'control'/name).write_bytes(source.read_bytes())
        else:(path/'control'/name).write_text('# frozen protocol\n')
    io.write(path/'frozen_hashes.json',{'runtime/qa/script_core/backend.py':io.sha(path/'runtime/qa/script_core/backend.py')})
    return inputs,payload


class MergedLazyExperimentTests(unittest.TestCase):
    def test_rejects_f_parent_and_unready_h_without_creating_output(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td)/'parent';out=Path(td)/'I';parent_fixture(parent)
            io.write(parent/'experiment.json',dict(arm='F_lazy_script'))
            with self.assertRaisesRegex(ValueError,'sealed H'):
                run.prepare(parent,out)
            self.assertFalse(out.exists())
            io.write(parent/'experiment.json',dict(arm=run.PARENT_ARM))
            (parent/'runs'/run.PARENT_ARM/'q0/result.json').unlink()
            with self.assertRaisesRegex(ValueError,'H sealed evidence and answer not ready'):
                run.prepare(parent,out)
            self.assertFalse(out.exists())

    def test_project_requires_exact_h_render_and_keeps_all_fixed_regions(self):
        with tempfile.TemporaryDirectory() as td:
            original,payload=parent_fixture(Path(td))
            result,memory,audit=run.project(original,payload)
            self.assertEqual(io.fixed(result),io.fixed(original))
            self.assertEqual(core.restore(memory),payload)
            self.assertEqual(audit['rendered_rows'],2)
            changed=deepcopy(original);changed['prompts'][0]=changed['prompts'][0].replace('C mixes.','Other wording',1)
            with self.assertRaisesRegex(ValueError,'actual H final input'):run.project(changed,payload)

    def test_prepare_is_read_only_to_h_and_produces_no_answers_or_database(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td)/'H';out=Path(td)/'I';parent_fixture(parent)
            before={str(p.relative_to(parent)):io.sha(p) for p in parent.rglob('*') if p.is_file()}
            run.prepare(parent,out)
            run.verify(out)
            after={str(p.relative_to(parent)):io.sha(p) for p in parent.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            self.assertFalse((out/'runs').exists())
            self.assertFalse((out/'database').exists())
            self.assertTrue((out/'final_inputs'/run.ARM/'q0.json').exists())
            self.assertEqual(io.sha(out/'runtime/qa/script_core/backend.py'),io.sha(parent/'runtime/qa/script_core/backend.py'))
            report=io.read(out/'report.json')
            self.assertEqual(report['arms'][run.ARM]['completed'],0)
            self.assertEqual(report['arms'][run.ARM]['pending'],1)
            self.assertIsNone(report['arms'][run.ARM]['accuracy'])
            self.assertEqual(report['paired_completed'],0)
            self.assertEqual(report['inherited_construction_seconds'],12.5)
            self.assertEqual(report['parent_memory_variant'],'H')
            self.assertEqual(report['primary_comparison'],'I_vs_H_generalized_lazy')
            self.assertTrue((out/'materialized/q0/H_field_plan.json').exists())
            with self.assertRaisesRegex(ValueError,'fresh I'):run.prepare(parent,out)

    def test_current_runner_refuses_to_write_legacy_i_report(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            io.write(out/'experiment.json',dict(parent_F_run='old F',version='merged-lazy-memory-I-v1'))
            io.write(out/'report.json',dict(historical=True))
            with self.assertRaisesRegex(ValueError,'frozen I-v1'):
                run.main(['report','--out',str(out)])
            self.assertEqual(io.read(out/'report.json'),dict(historical=True))

    def test_prepare_rejects_changed_source_seal_or_mismatched_h_answer_binding(self):
        for failure in ['seal','answer']:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as td:
                parent=Path(td)/'H';out=Path(td)/'I';parent_fixture(parent)
                if failure=='seal':io.write(parent/'materialized/q0/plan.json',dict(changed=True))
                else:
                    f=parent/'runs/H_generalized_lazy/q0/result.json';value=io.read(f)
                    value['input_sha256']='wrong';io.write(f,value)
                with self.assertRaises(ValueError):run.prepare(parent,out)

    def test_worker_routes_only_i_and_reports_paired_completed_results(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td)/'H';out=Path(td)/'I';parent_fixture(parent);run.prepare(parent,out)
            protocol=SimpleNamespace(io=SimpleNamespace())
            def qa_worker(destination,start,end,phase):
                self.assertEqual(protocol.io.ARMS,(run.ARM,))
                self.assertEqual((start,end,phase),(0,1,'I_merged_qa'))
                io.write(destination/'runs'/run.ARM/'q0/result.json',dict(
                    selected_option='0',error=None,input_tokens=80,seconds=1.2))
                return 0
            protocol.qa_worker=qa_worker
            with patch.object(run.importlib,'import_module',return_value=protocol):
                self.assertEqual(run.worker(out),0)
            report=io.read(out/'report.json')
            self.assertEqual(report['paired_completed'],1)
            self.assertEqual(report['comparisons']['I_vs_H_generalized_lazy']['corrected'],['q0'])
            self.assertEqual(report['paired'][run.PARENT_ARM]['correct'],0)
            self.assertEqual(report['arms'][run.ARM]['accuracy'],1)
            self.assertEqual(report['new_extraction_model_calls'],0)

    def test_frozen_entry_runs_report_without_repository_imports(self):
        with tempfile.TemporaryDirectory() as td:
            parent=Path(td)/'H';out=Path(td)/'I';parent_fixture(parent);run.prepare(parent,out)
            completed=subprocess.run([sys.executable,str(out/'control/run_merged_lazy_experiment.py'),
                'report','--out',str(out)],cwd=td,text=True,capture_output=True)
            self.assertEqual(completed.returncode,0,completed.stderr)
            self.assertEqual(json.loads(completed.stdout)['prepared_inputs'],1)
            # Even a later local source edit cannot silently alter frozen I.
            (out/'control/merged_lazy_memory.py').write_text('# changed\n')
            with self.assertRaisesRegex(ValueError,'Frozen I'):run.verify(out)


if __name__=='__main__':unittest.main()
