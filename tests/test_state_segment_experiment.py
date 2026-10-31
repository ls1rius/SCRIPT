import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script import run_state_segment_experiment as run
from script_core import state_segment_memory


class StateSegmentExperimentTests(unittest.TestCase):
    def test_d_preserves_c_sources_and_original_fixed_regions(self):
        payload=dict(observations=[dict(caption_id=f'C{t}',frame_index=t,sample_interval=[t,t+1],
            evidence=dict(states=[dict(entity='door',dimension='open_closed',value='closed')])) for t in [9,11,13]])
        original=dict(head='original system',prompts=['  Description: original\n\n###\n\n Questions: Q\nold memory and visuals'])
        source=dict(original,prompts=[original['prompts'][0].replace('original\n\n###',json.dumps(payload)+'\n\n###')])
        with patch.dict('sys.modules',state_segment_memory=state_segment_memory):
            projected,memory,audit=run.project(original,source)
        self.assertEqual(io.fixed(projected),io.fixed(original))
        self.assertEqual(state_segment_memory.restore(memory),payload)
        self.assertIn('Frames [9,14): State:',io.parts(projected)[1])
        self.assertEqual(audit['inferred_persistent_segments'],1)

    def test_report_pairs_d_with_reference_answers_and_costs_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'D';reference=Path(td)/'ABC'
            io.write(out/'cohort.json',['q0','q1','q2'])
            io.write(out/'evaluation_only.json',dict(q0='0',q1='1',q2='0'))
            io.write(out/'experiment.json',dict(reference_compact_run=str(reference)))
            for arm in ['A_original','B_compact_text','C_compact_text','D_state_segments']:
                root=out if arm.startswith('D_') else reference
                for uid in ['q0','q1']:
                    selected='0' if uid=='q0' or arm.startswith('D_') else '1'
                    io.write(root/'runs'/arm/uid/'result.json',dict(selected_option=selected,error=None,
                        seconds=1,input_tokens=10))
            for uid in ['q0','q1']:
                io.write(out/'input_seals'/f'{uid}.json',dict(inherited_extraction_seconds=100))
            report=run.report(out)
            self.assertFalse(report['complete'])
            self.assertEqual(report['arms']['D_state_segments']['correct'],1)
            self.assertIsNone(report['arms']['D_state_segments']['accuracy'])
            self.assertEqual(report['common_completed'],2)
            self.assertEqual(report['comparisons']['D_vs_C_compact_text']['regressed'],['q1'])
            self.assertEqual(report['inherited_extraction_seconds_completed_D'],200)


if __name__=='__main__':
    unittest.main()
