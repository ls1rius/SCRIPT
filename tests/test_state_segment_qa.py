from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from script.controlled_memory_io import fixed, parts
from script_core import adaptive_transition as partial, partial_temporal
from script_core.budget import ToolFailure, ResourceExhausted
from script_core.config import digest, load_config, validate_config
from script_core.state_segment_memory import restore
from script_core.temporal_pipeline import qa_context_inputs


def fixture():
    packet=dict(question=dict(question_id='q',video_id='v',question='Is the door closed?',
        options=['yes','no','unknown','open','absent']),captions=[dict(id=f'C{i}',
        interval=[i,i+1],text='#C The door is closed.') for i in range(3)])
    memory=partial.new_memory('v',packet['captions'],'source')
    partial.rebuild_indexes(memory)
    visual=[dict(description='A brass handle is visible.',source_frame_ids=['F0'],interval=[0,0])]
    qa=qa_context_inputs(dict(packet['question'],id='q'),packet['captions'],[0,1,2],
        partial.memory_context(memory),visual_observations=visual)
    bundle=dict(packet_hash=digest(packet),memory=memory,opened_scenes=[],selected_seconds=[0,1,2],
        supplemental_visual=dict(observations=visual,frame_ids=['F0']),source_incomplete=[],
        preparation_seconds=12.,qa_input=qa)
    return packet,bundle


class Backend:
    def __init__(self, overflow=False, exhausted=False):
        self.calls=[];self.overflow=overflow;self.exhausted=exhausted

    def call(self,op,inp,*args):
        self.calls.append((op,deepcopy(inp)))
        if op=='semantic_memory_extract':
            if self.exhausted:raise ResourceExhausted('Source extraction budget exhausted')
            facts=[dict(source_id=u['id'],entity='door',dimension='open_closed',value='closed')
                for u in inp['units']] if inp['category']=='state' else []
            return dict(data=dict(facts=facts,uncertain_source_ids=[]))
        if op=='temporal_qa_check':
            if self.overflow:raise ToolFailure('Original QA plus retrieved entity context exceeds input allowance')
            return dict(input_tokens=250)
        if op=='videotree_qa':return dict(data=dict(selected_option='0'))
        raise AssertionError(op)


class StateSegmentQATests(unittest.TestCase):
    def test_d_is_explicit_and_default_profiles_use_f(self):
        for name in ('script','script_local'):
            cfg=load_config(f'configs/{name}.yaml')
            self.assertEqual(cfg['query']['temporal_qa_evidence'],'lazy_script')
            self.assertEqual(cfg['query']['state_segment_max_gap_seconds'],2)
        self.assertEqual(load_config('configs/script_temporal_full.yaml')['query']['temporal_qa_evidence'],'full')
        for value in (-1,True,float('nan')):
            cfg=load_config('configs/script.yaml');cfg['query']['state_segment_max_gap_seconds']=value
            with self.assertRaises(ValueError):validate_config(cfg)

    def test_online_d_merges_state_preserves_fixed_regions_and_answers_once(self):
        packet,bundle=fixture();before=deepcopy(bundle);backend=Backend()
        with tempfile.TemporaryDirectory() as folder:
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_D.yaml'),
                folder,dict(online_seconds=1500))
            memory=json.loads((Path(folder)/'state_segment_memory.json').read_text())
            audit=json.loads((Path(folder)/'state_segment_audit.json').read_text())
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(bundle,before)
        self.assertEqual(result['qa_evidence']['mode'],'state_segments')
        self.assertEqual(result['incomplete'],[])
        self.assertEqual(audit['source_rows'],3)
        self.assertEqual(audit['inferred_persistent_segments'],1)
        self.assertEqual([r['caption_id'] for r in restore(memory)['observations']],['C0','C1','C2'])
        answers=[inp for op,inp in backend.calls if op=='videotree_qa']
        self.assertEqual(len(answers),1)
        self.assertEqual(fixed(answers[0]),fixed(bundle['qa_input']))
        self.assertIn('Frames [0,3): State:',parts(answers[0])[1])
        extraction=[inp for op,inp in backend.calls if op=='semantic_memory_extract']
        self.assertTrue(extraction)
        self.assertTrue(all(set(inp)=={'category','units','source_type'} for inp in extraction))
        self.assertTrue(all(set(u)=={'id','text'} for inp in extraction for u in inp['units']))
        self.assertEqual({inp['category'] for inp in extraction},
            {'action','attribute','state','relation','change','result','event_link'})

    def test_context_overflow_has_no_baseline_fallback_or_answer(self):
        packet,bundle=fixture();backend=Backend(overflow=True)
        with tempfile.TemporaryDirectory() as folder:
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_D.yaml'),
                folder,dict(online_seconds=1500))
        self.assertIn('exceeds input allowance',result['error']['message'])
        self.assertEqual(sum(op=='temporal_qa_check' for op,_ in backend.calls),1)
        self.assertFalse(any(op=='videotree_qa' for op,_ in backend.calls))

    def test_exhausted_extraction_retains_all_caption_sources_and_reports_incomplete(self):
        packet,bundle=fixture();backend=Backend(exhausted=True)
        with tempfile.TemporaryDirectory() as folder:
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_D.yaml'),
                folder,dict(online_seconds=1500))
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(result['incomplete'][0]['stage'],'semantic_extraction')
        qa=next(inp for op,inp in backend.calls if op=='videotree_qa')
        self.assertIn('#C The door is closed.',parts(qa)[1])
        self.assertEqual(result['qa_evidence']['source_coverage']['covered_captions'],3)
        self.assertEqual(fixed(qa),fixed(bundle['qa_input']))


if __name__=='__main__':unittest.main()
