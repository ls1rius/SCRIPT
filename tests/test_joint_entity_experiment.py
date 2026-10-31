import ast
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from script import controlled_memory_io as io
from script.run_joint_entity_experiment import extract_source,patch_runtime,source_observations,report,ARM
from script_core import joint_entity_memory as core
from tests.test_joint_entity_memory import report as joint_report, source


class JointExperimentTests(unittest.TestCase):
    def test_one_joint_call_checks_all_categories_and_review_is_bounded(self):
        class Meter:
            def __init__(self,values):self.values=iter(values);self.calls=[]
            def json(self,operation,inputs,required):
                self.calls.append((operation,inputs,required));return next(self.values),None
        protocol=SimpleNamespace(is_infrastructure=lambda e:'cuda' in str(e).lower())
        good=joint_report(actions=[dict(actor='C',verb='moves',patient='book',body_part='left hand')])
        meter=Meter([good])
        result=extract_source(source('C moves the book with her left hand.'),meter,core,protocol)
        self.assertEqual(len(meter.calls),1);self.assertTrue(result['complete_simple'])
        bad=joint_report(actions=[dict(actor='C',verb='moves',patient='book',instrument='left hand')])
        meter=Meter([bad,good])
        result=extract_source(source('C moves the book with her left hand.'),meter,core,protocol)
        self.assertEqual(len(meter.calls),2);self.assertTrue(result['role_review'])
        self.assertIn('review',meter.calls[1][1])

    def test_runtime_patch_keeps_original_qa_dispatch_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);d=root/'runtime/script_core';d.mkdir(parents=True)
            (d/'schemas.py').write_text('def output_schema(operation, inputs):\n    return {}\n')
            (d/'prompts.py').write_text('CONTRACTS={}\n')
            code="def prefix(operation):\n    if operation in ('temporal_observe',):\n        pass\nclass LocalModels:\n    def call(self, operation, inputs):\n        return operation, inputs\n    def vlm(self):\n        if True:\n            original_op='x'\n            if original_op == 'optimized_plan':\n                pass\n"
            (d/'backend.py').write_text(code)
            core_path=root/'core.py';core_path.write_text('PROMPT="test"\n')
            semantic=root/'semantic.py';semantic.write_text('# validator\n')
            patch_runtime(root/'runtime',core_path,semantic)
            tree=ast.parse((d/'backend.py').read_text())
            call=next(n for n in tree.body if isinstance(n,ast.ClassDef)).body[0]
            self.assertEqual(ast.unparse(call),'def call(self, operation, inputs):\n    return (operation, inputs)')
            self.assertIn('e_joint_extract',(d/'schemas.py').read_text())

    def test_all_original_caption_times_and_source_words_are_checked(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)
            rows=[dict(caption_id='C0',frame_index=0,sample_interval=[0,1],evidence=dict(original_text='#C C holds a cup.'))]
            original=dict(prompts=['  Description: '+json.dumps(dict(observations=rows))+'\n\n###\n\n Questions: Q'])
            io.write(out/'source_observations/q.json',original)
            bundle=dict(units=[dict(id='U0',text='#C C holds a cup.',source_caption_ids=['C0'],source_intervals=[[0,1]])],read_caption_ids=['C0'])
            self.assertEqual(source_observations(out,'q',bundle)[0]['source_id'],'U0')
            bundle['units'][0]['source_intervals']=[[1,2]]
            with self.assertRaisesRegex(ValueError,'text or time'):
                source_observations(out,'q',bundle)

    def test_report_uses_same_question_pairs_and_source_only_flags(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'E';reference=Path(td)/'ABC';D=Path(td)/'D'
            io.write(out/'cohort.json',['q0','q1'])
            io.write(out/'evaluation_only.json',dict(q0='0',q1='1'))
            io.write(out/'experiment.json',dict(reference_compact_run=str(reference),D_run=str(D),normalization_parent=str(Path(td)/'parent')))
            for arm in ['A_original','B_compact_text','C_compact_text','D_state_segments',ARM]:
                root=out if arm==ARM else D if arm.startswith('D_') else reference
                io.write(root/'runs'/arm/'q0/result.json',dict(selected_option='0' if arm==ARM else '1',error=None,seconds=1,input_tokens=10))
            keys=['source_rows','entities','alias_links','ambiguous_mentions','merged_state_segments','original_clauses_retained',
                  'original_clauses_replaced','joint_model_calls','failed_source_units','role_reviews']
            audit={k:0 for k in keys};audit.update(merged_state_segments=1,joint_model_calls=2)
            io.write(out/'materialized/q0/audit.json',audit)
            r=report(out)
            self.assertFalse(r['complete']);self.assertEqual(r['common_completed'],1)
            self.assertEqual(r['comparisons']['E_vs_C_compact_text']['corrected'],['q0'])
            self.assertEqual(r['paired_source_only_subgroups']['with_state_merging'][ARM]['completed'],1)


if __name__=='__main__':unittest.main()
