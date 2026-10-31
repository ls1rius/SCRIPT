import ast
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

from script import controlled_memory_io as io
from script import run_generalized_entity_experiment as run
from script_core import generalized_entity_memory as core
from tests.test_generalized_entity_memory import report, source


class GeneralizedExperimentTests(unittest.TestCase):
    def test_extraction_is_source_only_and_open_state_reaches_storage(self):
        class Meter:
            def json(self, operation, inputs, required):
                self.operation, self.inputs = operation, inputs
                return report(states=[dict(entity='server', description='unreachable')]), None
        meter = Meter()
        value = run.extract_source(source('The server is unreachable.'), meter, core,
            SimpleNamespace(is_infrastructure=lambda e: False))
        self.assertEqual(meter.operation, core.OPERATION)
        self.assertEqual(set(meter.inputs), {'unit','source_type'})
        self.assertEqual(value['accepted']['facts'][0]['description'], 'unreachable')
        self.assertNotIn('dimension', value['accepted']['facts'][0])

    def test_runtime_patch_keeps_qa_dispatch_and_parent_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root/'script_core'
            d.mkdir()
            (d/'schemas.py').write_text('def output_schema(operation, inputs):\n    return {}\n')
            (d/'prompts.py').write_text('CONTRACTS={}\n')
            code="def prefix(operation):\n    if operation in ('e_joint_extract',):\n        pass\nclass LocalModels:\n    def call(self, operation, inputs):\n        return operation, inputs\n    def vlm(self):\n        if True:\n            original_op='x'\n            if original_op == 'e_joint_extract':\n                pass\n"
            (d/'backend.py').write_text(code)
            (d/'joint_entity_memory.py').write_text('# frozen E core\n')
            original = io.sha(d/'joint_entity_memory.py')
            core_path = root/'G.py'
            core_path.write_text('PROMPT="G"\n')
            run.patch_runtime(root, core_path)
            tree = ast.parse((d/'backend.py').read_text())
            call = next(n for n in tree.body if isinstance(n,ast.ClassDef)).body[0]
            self.assertEqual(ast.unparse(call), 'def call(self, operation, inputs):\n    return (operation, inputs)')
            self.assertEqual(io.sha(d/'joint_entity_memory.py'), original)
            self.assertIn('g_joint_extract', (d/'schemas.py').read_text())

    def test_report_pairs_each_reference_independently(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);out=root/'G';reference=root/'ABC';E=root/'E';D=root/'D';F=root/'F'
            io.write(out/'cohort.json',['q0','q1'])
            io.write(out/'evaluation_only.json',dict(q0='0',q1='1'))
            io.write(out/'experiment.json',dict(reference_compact_run=str(reference),parent_E_run=str(E),D_run=str(D),F_run=str(F)))
            roots={a:reference for a in ['A_original','B_compact_text','C_compact_text']}
            roots.update(E_joint_entities=E,D_state_segments=D,F_lazy_script=F)
            roots[run.ARM]=out
            for arm,folder in roots.items():
                for uid,truth in [('q0','0'),('q1','1')]:
                    if arm=='F_lazy_script' and uid=='q1':continue
                    io.write(folder/'runs'/arm/uid/'result.json',dict(selected_option=truth,error=None,seconds=1,input_tokens=10))
            value=run.report(out)
            self.assertTrue(value['complete'])
            self.assertEqual(value['common_completed'],2)
            self.assertEqual(value['comparisons']['G_vs_E_joint_entities']['paired'],2)
            self.assertEqual(value['comparisons']['G_vs_F_lazy_script']['paired'],1)
            self.assertEqual(value['paired']['E_joint_entities'][run.ARM]['completed'],2)
            self.assertEqual(value['state_representation'],'entity_description')


if __name__ == '__main__':
    unittest.main()
