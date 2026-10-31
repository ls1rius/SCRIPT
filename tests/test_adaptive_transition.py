"""The transition is the preserved first version, independently runnable from frame-guided 4."""
from copy import deepcopy
import inspect
from pathlib import Path
import tempfile
import unittest

from script_core import adaptive_transition as transition
from script_core.config import load_config
from script_core.prompts import CONTRACTS
from script_core.schemas import output_schema, validate_output


class TransitionTests(unittest.TestCase):
    def test_repaired_transition_keeps_whole_scene_partial_algorithm_separate(self):
        self.assertEqual(transition.VERSION, 'query-partial-v2')
        source = inspect.getsource(transition.run)
        self.assertNotIn('adaptive_extract', source)
        self.assertNotIn('expand_frames', source)
        self.assertNotIn('scene_correspondence_lineage', source)
        self.assertIn('transition_extract', source)

    def test_transition_planner_cannot_accidentally_request_new_scheme4_frame_actions(self):
        schema = output_schema('transition_next', dict(captions=[], entities=[], allowed_scene_ids=['S0']))
        plan = dict(sufficient=False, reason='need evidence', gap='object', action='read_scene', scene_id='S0',
                    caption_ids=[], entity_ids=[], focus=[], fields=[])
        validate_output(plan, schema)
        for action in ('expand_frames', 'link_entities'):
            with self.assertRaises(ValueError):
                validate_output(dict(plan, action=action), schema)

    def test_whole_scene_is_read_but_only_relevant_entity_is_extracted_then_reloaded(self):
        captions = [dict(id=f'C{i}', interval=[i, i+1], text=f'C holds the red box at step {i}.') for i in range(8)]
        packet = dict(captions=captions, question=dict(question_id='q1', video_id='v',
            question='What happens to the box?', options=['a', 'b', 'c', 'd', 'e']))
        directory = dict(captions=captions, scenes=[dict(id='S0', interval=[0, 8], center=[1., 0.],
            caption_ids=[c['id'] for c in captions], chronological_order=list(range(8)), description='box')], pointers=[])
        cfg = load_config()
        cfg['query']['temporal_sparse_visual'] = False
        self_outer = self
        class Backend:
            def __init__(self): self.extracted = False; self.calls = []
            def call(self, op, inp, timeout):
                self.calls.append((op, deepcopy(inp)))
                if op == 'scene_scope': return dict(data=dict(scope='local', reason='local'))
                if op == 'clip_text': return dict(vectors=[[1., 0.]]*6)
                if op == 'transition_next':
                    self_outer.assertEqual(len(inp['captions']), 8)
                    return dict(data=dict(sufficient=self.extracted, reason='read evidence', gap='box detail',
                        action='stop' if self.extracted else 'extract', scene_id='stop',
                        caption_ids=[] if self.extracted else ['C0'], entity_ids=[], focus=['box'],
                        fields=['color'] if inp['entities'] else ['state']))
                if op == 'transition_extract':
                    self.extracted = True
                    self_outer.assertEqual([c['id'] for c in inp['captions']], ['C0'])
                    attrs = [dict(field='color', value='red', source_caption_ids=['C0'])] if inp['entities'] else []
                    return dict(data=dict(entities=[dict(entity_ref='E0' if inp['entities'] else 'new', label='box',
                        source_caption_ids=['C0'], attributes=attrs, observations=[dict(caption_id='C0', kind='state')])],
                        relations=[], unresolved=''))
                if op == 'temporal_qa_check': return dict(input_tokens=100)
                if op == 'videotree_qa':
                    self_outer.assertIn('step 7', inp['prompts'][-1])
                    return dict(data=dict(selected_option='0'))
                raise AssertionError(op)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = Backend()
            first = transition.run(packet, directory, [], backend, cfg, root/'first',
                dict(online_seconds=600, max_scene_visits=4), root/'memory.json')
            self.assertIsNone(first['error'], first['error'])
            self.assertEqual(first['selected_seconds'], list(range(8)))
            self.assertEqual(first['entity_count'], 1)
            backend.extracted = False
            packet['question'].update(question_id='q2', question='What color is the box?')
            second = transition.run(packet, directory, [], backend, cfg, root/'second',
                dict(online_seconds=600, max_scene_visits=4), root/'memory.json')
            self.assertIsNone(second['error'], second['error'])
            self.assertEqual(second['reused_entity_count'], 1)
            ops = {op for op, _ in backend.calls}
            self.assertFalse(ops & {'adaptive_next', 'adaptive_extract', 'caption_temporal_observe', 'scene_correspondence_lineage'})


if __name__ == '__main__':
    unittest.main()
