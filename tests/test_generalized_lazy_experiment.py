import ast
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script import run_generalized_lazy_experiment as run
from script import generalized_lazy_execution as execution
from script_core import generalized_lazy_memory as core
from script_core import backend, context, schemas, batch_extraction as batch
from tests.test_generalized_entity_memory import report
from tests.test_generalized_lazy_memory import unit, row


ROOT=Path(__file__).resolve().parents[1]
F_BACKEND="""def prefix(operation):
    if operation in ('f_plan_fields', 'f_extract_fields', 'e_joint_extract'):
        pass
class LocalModels:
    def call(self, operation, inputs):
        return operation, inputs
    def vlm(self):
        original_op='x'
        if original_op in ('f_plan_fields', 'f_extract_fields'):
            pass
"""


def stub_runtime(path):
    destination=path/'script_core';destination.mkdir(parents=True)
    (destination/'schemas.py').write_text('def output_schema(operation, inputs):\n    return {}\n')
    (destination/'prompts.py').write_text('CONTRACTS={}\n')
    (destination/'backend.py').write_text(F_BACKEND)
    (destination/'joint_entity_memory.py').write_text('# frozen parent identity layer\n')
    (destination/'lazy_script_memory.py').write_text('# frozen F storage\n')


class Processor:
    def apply_chat_template(self,messages,**kwargs):
        self.messages=deepcopy(messages)
        return json.dumps(messages,ensure_ascii=False)


class GeneralizedLazyExperimentTests(unittest.TestCase):
    def test_runtime_patch_adds_h_without_changing_f_sources_or_qa_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            runtime=Path(td);stub_runtime(runtime)
            d=runtime/'script_core'
            original={name:io.sha(d/name) for name in ['joint_entity_memory.py','lazy_script_memory.py']}
            run.patch_runtime(runtime,ROOT)
            tree=ast.parse((d/'backend.py').read_text())
            call=next(n for n in tree.body if isinstance(n,ast.ClassDef)).body[0]
            self.assertEqual(ast.unparse(call),'def call(self, operation, inputs):\n    return (operation, inputs)')
            for name,value in original.items():self.assertEqual(io.sha(d/name),value)
            self.assertIn('h_extract_fields',(d/'schemas.py').read_text())
            self.assertIn("CONTRACTS['h_plan_fields']",(d/'prompts.py').read_text())
            self.assertIn('f_extract_fields',(d/'backend.py').read_text())

    def test_cpu_prepare_freezes_fresh_h_without_copying_cache_or_answers(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);reference=root/'F';out=root/'H'
            policy=dict(arm='F_lazy_script',reference_compact_run=str(root/'ABC'),D_run=str(root/'D'),
                E_run=str(root/'E'),normalization_parent=str(root/'parent'))
            for name,value in {'experiment.json':policy,'cohort.json':['q0'],
                    'evaluation_only.json':dict(q0='0'),'historical_only.json':dict(q0=dict(selected_option='0')),
                    'qa_config.json':{},'qa_expected_artifacts.json':{},'validation/functional.json':dict(passed=True),
                    'packets/q0.json':dict(packet=dict(question=dict(video_id='v0'))),
                    'final_inputs/A_original/q0.json':{},'normalization_sources/q0.json':{},
                    'source_observations/q0.json':{},'runs/F_lazy_script/q0/result.json':dict(selected_option='0',error=None,seconds=1,input_tokens=9)}.items():io.write(reference/name,value)
            stub_runtime(reference/'runtime/qa')
            for name in ['compact_protocol.py','controlled_memory_io.py','qa_evidence_view.py',
                    'run_joint_entity_experiment.py','run_compact_resident.py']:
                p=reference/'control'/name;p.parent.mkdir(exist_ok=True);p.write_text('# frozen dependency\n')
            p=reference/'database/script.sqlite3';p.parent.mkdir();p.write_bytes(b'legacy-cache-must-not-be-copied')
            io.write(reference/'frozen_hashes.json',{str(p.relative_to(reference)):io.sha(p)
                for p in reference.rglob('*') if p.is_file()})
            before={str(p.relative_to(reference)):io.sha(p) for p in reference.rglob('*') if p.is_file()}
            run.prepare(reference,out)
            run.verify(out)
            self.assertEqual(before,{str(p.relative_to(reference)):io.sha(p) for p in reference.rglob('*') if p.is_file()})
            self.assertFalse((out/'database').exists());self.assertFalse((out/'runs').exists())
            self.assertEqual(io.read(out/'experiment.json')['database'],'database/h_script.sqlite3')
            value=io.read(out/'report.json')
            self.assertFalse(value['complete']);self.assertEqual(value['arms'][run.ARM]['completed'],0)
            self.assertIsNone(value['arms'][run.ARM]['accuracy'])
            self.assertEqual(value['comparisons']['H_vs_F_lazy_script']['paired'],0)
            (out/'runtime/qa/script_core/generalized_lazy_memory.py').write_text('# changed')
            with self.assertRaisesRegex(ValueError,'Frozen H'):run.verify(out)

    def test_report_uses_separate_paired_denominators_and_full_cost_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);out=root/'H'
            io.write(out/'cohort.json',['q0','q1']);io.write(out/'evaluation_only.json',dict(q0='0',q1='1'))
            io.write(out/'experiment.json',dict(reference_compact_run=str(root/'ABC'),D_run=str(root/'D'),
                E_run=str(root/'E'),parent_F_run=str(root/'F'),G_run=str(root/'G'),unique_videos=2))
            for arm,path in [(run.ARM,out),('F_lazy_script',root/'F'),('G_generalized_entities',root/'G')]:
                for uid,label in [('q0','0'),('q1','1')]:
                    if arm=='G_generalized_entities' and uid=='q1':continue
                    io.write(path/'runs'/arm/uid/'result.json',dict(selected_option=label,error=None,seconds=1,input_tokens=12))
            io.write(out/'materialized/q0/audit.json',dict(cache={},warm_read_identical=True,visible_source_rows=2,
                selected_source_rows=2,retained_original_rows=1,open_state_facts=1,actorless_action_facts=1))
            p=out/'materialized/q0/calls.jsonl';p.write_text(json.dumps(dict(actual_cost=7))+'\n')
            value=run.report(out)
            self.assertTrue(value['complete']);self.assertEqual(value['comparisons']['H_vs_F_lazy_script']['paired'],2)
            self.assertEqual(value['comparisons']['H_vs_G_generalized_entities']['paired'],1)
            self.assertEqual(value['totals']['open_state_facts'],1)
            self.assertEqual(value['source_coverage']['visible_source_rows'],2)
            self.assertEqual(value['measured_seconds_by_stage']['materialized'],7)

    def test_h_batch_prompt_matches_serial_backend_and_keeps_source_schemas_isolated(self):
        class Captured(Exception):pass
        class CapturingProcessor(Processor):
            def apply_chat_template(self,messages,**kwargs):
                super().apply_chat_template(messages,**kwargs);raise Captured()
        rt=SimpleNamespace(backend=backend,context=context,schemas=schemas,memory=core,
            prompts=SimpleNamespace(SYSTEM=backend.SYSTEM))
        for text,fields in [('The server is unreachable.',['states']),('The box was moved with a crane.',['actions.instrument'])]:
            inputs=dict(unit=dict(id='U7',text=text),requested_fields=fields,source_type='caption')
            processor=CapturingProcessor();models=backend.LocalModels(dict(models=dict(max_input_tokens=10000)))
            models.load=lambda _:(None,processor)
            with patch.dict(backend.CONTRACTS,{core.EXTRACT:core.EXTRACT_PROMPT}),patch.object(schemas,'output_schema',side_effect=core.output_schema):
                with self.assertRaises(Captured):models.vlm(core.EXTRACT,inputs)
            result=batch.render_request(inputs,rt,Processor())
            self.assertEqual(result['messages'],processor.messages)
            self.assertEqual(result['inverse'],{'ID0':'U7'})
            if 'server' in text:self.assertNotIn('crane',json.dumps(result['schema']))
            else:self.assertNotIn('unreachable',json.dumps(result['schema']))

    def test_batched_open_state_and_unknown_actor_pass_real_acceptance_and_warm_read(self):
        units=[unit('s','The server is unreachable.',0),unit('a','The box was moved with a crane.',2)]
        class Meter:
            def __init__(self):self.ledger=SimpleNamespace(calls=[])
            def call(m,operation,inputs):
                m.ledger.calls.append(dict(operation=operation,parameters=deepcopy(inputs),actual_cost=3))
                values=[report(states=[dict(entity='server',description='unreachable')]),
                    report(actions=[dict(actor=None,verb='moved',roles=[dict(role='patient',value='box'),dict(role='instrument',value='crane')])])]
                return dict(items=[dict(data=v) for v in values]),None
        with tempfile.TemporaryDirectory() as td,core.ScriptDB(Path(td)/'h.sqlite3') as store:
            meter=Meter();extractor=execution.SourceExtractor(meter,core,SimpleNamespace(is_infrastructure=lambda _:False),run.extract_source)
            results,_=execution.ensure_batched(store,'v',units,list(map(row,units)),['states','actions.base','actions.instrument'],extractor)
            self.assertEqual(results['s']['accepted']['facts'][0]['description'],'unreachable')
            self.assertNotIn('actor',results['a']['accepted']['facts'][0])
            warm,stats=store.ensure('v',units,list(map(row,units)),['states','actions.base','actions.instrument'],lambda *_:self.fail('repeated call'))
            self.assertEqual(results,warm);self.assertEqual(stats['missing_fields'],0)
            self.assertEqual(execution.extraction_counts(meter.ledger.calls,core.EXTRACT),
                dict(field_extraction_model_calls=1,field_extraction_source_requests=2,extraction_batches=1))


if __name__=='__main__':unittest.main()
