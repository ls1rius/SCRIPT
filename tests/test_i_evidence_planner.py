from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script import run_i_evidence_planner_experiment as run
from script import run_merged_lazy_experiment as i_run
from script import run_evidence_planner_experiment as engine
from script_core import i_evidence_planner as core, merged_lazy_memory as merged
from tests.test_merged_lazy_experiment import parent_fixture
from tests.test_evidence_planner_experiment import Tokenizer


def setup(root):
    h, parent, out = root/'H', root/'I', root/'L'
    parent_fixture(h)
    io.write(h/'qa_config.json',dict(models=dict(vlm='local-model',precision='bfloat16',
        max_input_tokens=16384,generation=dict(do_sample=False,max_new_tokens=1024))))
    io.write(h/'qa_expected_artifacts.json',dict(vlm=dict(path='local-model',files={})))
    io.write(h/'packets/q0.json',dict(packet=dict(question=dict(question_id='q0',video_id='v',
        question='Which tool?',options=['cloth','brush','knife','spoon','paper']))))
    for name in ('model.py','prompts.py'):(h/'runtime/qa'/name).write_text('# frozen runtime\n')
    i_run.prepare(h,parent)
    path=parent/'final_inputs'/run.PARENT_ARM/'q0.json'
    io.write(parent/'runs'/run.PARENT_ARM/'q0/result.json',dict(question_id='q0',selected_option=None,
        error=dict(type='ToolFailure',message='old answer-format failure'),input_sha256=io.sha(path),seconds=1.))
    i_run.report(parent)
    io.write(parent/'queue_status.json',dict(state='complete'))
    return parent,out


class IPlannerTests(unittest.TestCase):
    def test_open_states_actorless_actions_and_adjacent_samples_survive(self):
        case=core.functional_probe(io.parts);pool=case['pool'];before=deepcopy(pool['merged_archive'])
        text,audit=core.render(pool,core.keep_all(pool))
        self.assertIn('unspecified actor',text)
        self.assertIn('unreachable',text)
        self.assertIn('[0,2)',text)
        self.assertNotIn('dimension=',text)
        self.assertNotIn('actor=',text)
        self.assertEqual(pool['merged_archive'],before)
        self.assertEqual(audit['parent_memory_variant'],'I')
        self.assertFalse(audit['inferred_continuity'])
        self.assertEqual(audit['groups'][0]['occurrences'][0]['interval'],[0,1])
        self.assertEqual(audit['groups'][0]['occurrences'][1]['interval'],[1,2])
        self.assertIn('H fact',pool['source_counts'])
        self.assertNotIn('F fact',pool['source_counts'])

    def test_gaps_negation_and_original_group_archive_are_preserved(self):
        payload=dict(observations=[dict(caption_id=f'C{i}',frame_index=t,sample_interval=[t,t+1],
            evidence=dict(original_text=body)) for i,(t,body) in enumerate([
                (0,'C moves box.'),(1,'C moves box.'),(4,'C moves box.'),(5,'C never moves box.')])])
        memory=merged.materialize(payload)
        original=dict(head='same',prompts=['  Description: '+merged.render(memory)[0]+'\n\n###\n\n Questions: q'])
        pool=core.build_pool(memory,original,io.parts)
        text,audit=core.render(pool,core.keep_all(pool))
        self.assertIn('[0,2), [4,5)',text)
        self.assertIn('C never moves box.',text)
        self.assertEqual(merged.restore(pool['merged_archive']),payload)
        self.assertFalse(audit['inferred_event_counts'])
        self.assertFalse(audit['inferred_continuity'])
        changed=deepcopy(original);changed['prompts'][0]=changed['prompts'][0].replace('C moves box.','invented',1)
        with self.assertRaisesRegex(ValueError,'actual I final input'):core.build_pool(memory,changed,io.parts)

    def test_rejects_fixed_states_and_keeps_complete_source_audit(self):
        with self.assertRaisesRegex(ValueError,'open state'):
            core.fact_sentence('states',dict(entity='box',dimension='wetness',value='wet'))
        case=core.functional_probe(io.parts);pool=case['pool']
        decisions={r['id']:'skip' if 'chair' in r['text'] else 'keep' for r in pool['records']}
        _,audit=core.render(pool,core.decode_plan(decisions,pool))
        self.assertEqual(set(audit['skipped_ids']),set(case['skipped_ids']))
        self.assertEqual(audit['total_records'],len(pool['records']))

    def test_prepare_retains_parent_format_failure_and_exact_i_control(self):
        with tempfile.TemporaryDirectory() as td:
            parent,out=setup(Path(td))
            before={str(p.relative_to(parent)):io.sha(p) for p in parent.rglob('*') if p.is_file()}
            run.prepare(parent,out,Tokenizer());engine.verify(out)
            self.assertEqual(before,{str(p.relative_to(parent)):io.sha(p) for p in parent.rglob('*') if p.is_file()})
            self.assertEqual(io.sha(out/'I_control/q0.json'),io.sha(parent/'final_inputs'/run.PARENT_ARM/'q0.json'))
            policy=io.read(out/'experiment.json')
            self.assertEqual(policy['parent_answer_errors_retained'],1)
            self.assertEqual(policy['parent_memory_variant'],'I')
            self.assertIsNone(policy['after_queue'])
            self.assertFalse((out/'runs').exists())
            self.assertEqual(io.read(out/'report.json')['completed'],0)
            self.assertIn('inherited_I_construction_seconds',io.read(out/'report.json'))
            self.assertNotIn('inherited_F_construction_seconds',io.read(out/'report.json'))

    def test_seal_tampering_legacy_i_and_overflow_fail_before_output(self):
        for kind in ('legacy','seal','overflow'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as td:
                parent,out=setup(Path(td));tokenizer=Tokenizer()
                if kind=='legacy':
                    io.write(parent/'experiment.json',dict(arm=run.PARENT_ARM,parent_memory_variant='F'))
                    manifest=io.read(parent/'frozen_hashes.json');manifest['experiment.json']=io.sha(parent/'experiment.json');io.write(parent/'frozen_hashes.json',manifest)
                elif kind=='seal':
                    io.write(parent/'materialized/q0/memory.json',dict(tampered=True))
                else:
                    tokenizer.apply_chat_template=lambda *a,**k:[0]*16385
                with self.assertRaises(ValueError):run.prepare(parent,out,tokenizer)
                self.assertFalse(out.exists())

    def test_real_tokenizer_mock_worker_is_gold_blind_and_l_uses_all_new_arms(self):
        import torch
        from transformers import AutoTokenizer
        project=Path(__file__).resolve().parents[1]
        tokenizer=AutoTokenizer.from_pretrained(project/'checkpoints/Qwen/Qwen2.5-VL-7B-Instruct',local_files_only=True)
        calls=[]
        class Network:
            device='cpu'
            generation_config=SimpleNamespace(eos_token_id=[151645,151643])
            def generate(self,input_ids,**kwargs):
                calls.append(kwargs)
                text=tokenizer.decode(input_ids[0],skip_special_tokens=True)
                if '{"task":' in text:
                    task,_=json.JSONDecoder().raw_decode(text[text.index('{"task":'):])
                    decisions={r['id']:'skip' if ('unused chair' in r['text'] or 'The chair is blue.' == r['text']
                        or (task['task']['question'].startswith('Which tool does C use') and r['id']=='R1')) else 'keep' for r in task['evidence']}
                    raw=json.dumps(decisions)
                else:
                    raw='prediction: D' if 'Options: A. brush; B. knife; C. spoon; D. cloth; E. paper' in text else 'prediction: A'
                return torch.cat([input_ids,torch.tensor([tokenizer.encode(raw,add_special_tokens=False)+[151643]])],dim=1)
        class Model:
            def __init__(self,*a,**k):self.tokenizer=tokenizer;self.model=Network();self.max_new_tokens=1024
        with tempfile.TemporaryDirectory() as td:
            parent,out=setup(Path(td));run.prepare(parent,out,Tokenizer())
            read=io.read
            def gold_blind(path):
                if Path(path).name=='evaluation_only.json':raise AssertionError('Gold leaked to L worker')
                return read(path)
            with patch.dict(sys.modules,{'model':SimpleNamespace(Qwen=Model)}),patch.object(engine.io,'read',side_effect=gold_blind):
                self.assertEqual(engine.worker(out),0)
            self.assertEqual(len(calls),11)
            self.assertTrue(read(out/'validation/parent_memory_functional.json')['passed'])
            summary=read(out/'report.json');self.assertTrue(summary['complete'])
            self.assertEqual(set(summary['arms']),set(run.ARMS))
            self.assertEqual(summary['comparisons'][run.ARM+'_vs_I_control']['L_correct'],1)
            self.assertIn('mean_L_planner_plus_final_seconds',summary)
            self.assertTrue((out/'runs/q0/L_input.json').exists())
            self.assertFalse((out/'runs/q0/K_input.json').exists())
            self.assertTrue(all('prefix_allowed_tokens_fn' in c for c in calls))


if __name__=='__main__':unittest.main()
