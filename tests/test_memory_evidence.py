"""Final-input interventions preserve cited facts, source limits and one QA."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from script_core.config import load_config, validate_config
from script_core.memory_evidence import supporting_quotes
from script_core.temporal_pipeline import fit_qa_context, qa_context_inputs
from script_core import partial_temporal
from tests.test_partial_temporal import fixture, Backend
from tests.test_temporal_qa_context import CheckingMeter


class MemoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.captions=[dict(id=f'C{i}',interval=[i,i+1],text=text) for i,text in enumerate([
            'C holds a dusty book.', 'C picks up a cloth.', 'C wipes the book with the cloth.',
            'C puts the clean book down.', 'DISTRACTOR C walks around the room.',
            'UNREAD C washes a cup.'])]
        self.query=dict(id='q',question='What does C do?',options=['clean','organize','read','repair','move'])
        self.memory=dict(source_type='caption',
            entities=[dict(id='E0',label='book',attributes=[])],
            states=[dict(id='T0',entity_id='E0',description='dusty',source_caption_ids=['C0'],relation_ids=[]),
                    dict(id='T1',entity_id='E0',description='clean',source_caption_ids=['C3'],relation_ids=[])],
            transitions=[dict(id='X0',before='T0',after='T1',
                cause=dict(action='wipes',source_caption_ids=['C2']))],
            relations=[],identities=[],lineage=[],
            context_selection=dict(selected_entities=1,selected_states=2))

    def test_quotes_are_original_bounded_and_linked_to_temporal_endpoints(self):
        before=deepcopy((self.captions,self.memory))
        audit=supporting_quotes(self.captions,list(range(5)),self.memory,maximum=2)
        self.assertEqual(audit['quoted_caption_ids'],['C0','C3'])
        self.assertIn('C2',audit['omitted_caption_ids'])
        self.assertIn('transitions:X0:cause',audit['facts_without_quoted_support'])
        for quote in audit['quotes']:
            original=next(c for c in self.captions if c['id']==quote['caption_id'])
            self.assertEqual(quote['text'],original['text'])
            self.assertEqual(quote['interval'],original['interval'])
            self.assertIn('transitions:X0',quote['supports'])
        self.assertEqual((self.captions,self.memory),before)

    def test_cause_tool_quote_is_kept_as_its_own_supported_relation(self):
        self.memory['relations']=[dict(id='R0',subject='E0',object='E0',
            predicate='wiped with cloth',source_caption_ids=['C2'])]
        audit=supporting_quotes(self.captions,list(range(5)),self.memory,maximum=3)
        self.assertEqual(audit['quoted_caption_ids'],['C0','C2','C3'])
        self.assertIn('relations:R0',audit['quotes'][1]['supports'])
        self.assertNotIn('C4',audit['quoted_caption_ids'])

    def test_transition_keeps_action_tool_evidence_without_an_extra_relation(self):
        audit=supporting_quotes(self.captions,list(range(5)),self.memory,maximum=3)
        self.assertEqual(audit['quoted_caption_ids'],['C0','C2','C3'])
        self.assertIn('transitions:X0:cause',audit['quotes'][1]['supports'])

    def test_unread_and_foreign_citations_fail_instead_of_leaking_text(self):
        for cid in ('C5','FOREIGN'):
            memory=deepcopy(self.memory);memory['states'][0]['source_caption_ids']=[cid]
            with self.assertRaisesRegex(ValueError,'unread or unknown'):
                supporting_quotes(self.captions,list(range(5)),memory)

    def test_memory_prompt_omits_distractors_and_preserves_options_and_visuals(self):
        visual=[dict(description='A cloth touches the book.',source_frame_ids=['F2'],interval=[2,2])]
        inp=qa_context_inputs(self.query,self.captions,list(range(5)),self.memory,
            visual_observations=visual,evidence_mode='memory_quotes',max_quotes=3)
        prompt=inp['prompts'][0]
        self.assertTrue(prompt.startswith('Temporal entity memory'))
        self.assertNotIn('DISTRACTOR',prompt)
        self.assertNotIn('UNREAD',prompt)
        self.assertIn('A cloth touches the book.',prompt)
        self.assertIn('"A":"clean","B":"organize","C":"read","D":"repair","E":"move"',prompt)
        self.assertIn('single option letter',inp['head'])

    def test_context_pruning_reselects_quotes_from_surviving_facts(self):
        memory=deepcopy(self.memory)
        memory['entities'].append(dict(id='E1',label='x'*5000,attributes=[]))
        memory['states'].append(dict(id='T2',entity_id='E1',source_caption_ids=['C4'],relation_ids=[]))
        before=deepcopy(memory)
        meter=CheckingMeter(4900)
        inp,view,tokens=fit_qa_context(self.query,self.captions,list(range(5)),memory,meter,
            evidence_mode='memory_quotes',max_quotes=3)
        self.assertEqual([e['id'] for e in view['entities']],['E0'])
        self.assertNotIn('DISTRACTOR',inp['prompts'][0])
        self.assertLessEqual(tokens,meter.limit)
        self.assertEqual(memory,before)

    def test_pipeline_keeps_preparation_fixed_and_answers_once(self):
        packet,bundle=fixture();before=deepcopy(bundle);backend=Backend()
        cfg=load_config('configs/script_memory_quotes.yaml')
        with tempfile.TemporaryDirectory() as folder:
            result=partial_temporal.run(packet,bundle,backend,cfg,folder,dict(online_seconds=1500))
            stored=json.loads((Path(folder)/'qa_context.json').read_text())
        self.assertIsNone(result['error'],result['error'])
        self.assertEqual(bundle,before)
        self.assertEqual(result['selected_seconds'],bundle['selected_seconds'])
        self.assertEqual(result['qa_evidence']['mode'],'memory_quotes')
        self.assertTrue(result['method'].endswith('-memory-quotes-v1'))
        self.assertEqual(result['qa_evidence']['quoted_caption_ids'],stored['evidence_selection']['quoted_caption_ids'])
        self.assertEqual(sum(op=='videotree_qa' for op,_ in backend.calls),1)
        prompt=next(inp for op,inp in backend.calls if op=='videotree_qa')['prompts'][0]
        self.assertNotIn('A wall is white.',prompt)
        self.assertEqual(result['temporal_pair_count'],3)
        self.assertGreater(result['identity_comparisons'],0)

    def test_new_mode_still_checks_frozen_upstream_qa_evidence(self):
        packet,bundle=fixture();backend=Backend()
        bundle['qa_input']['prompts'][0]=bundle['qa_input']['prompts'][0].replace('Alice holds a closed box.','ALTERED SOURCE')
        with tempfile.TemporaryDirectory() as folder:
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_memory_quotes.yaml'),
                folder,dict(online_seconds=1500))
        self.assertIn('Paired QA captions',result['error']['message'])
        self.assertFalse(any(op=='videotree_qa' for op,_ in backend.calls))

    def test_empty_memory_is_explicit_and_does_not_reintroduce_raw_descriptions(self):
        memory=dict(source_type='caption',entities=[],states=[],transitions=[],relations=[],identities=[],lineage=[])
        audit=supporting_quotes(self.captions,list(range(5)),memory)
        self.assertTrue(audit['empty_memory']);self.assertTrue(audit['no_supporting_quotes'])
        inp=qa_context_inputs(self.query,self.captions,list(range(5)),memory,evidence_mode='memory_quotes')
        self.assertNotIn('C holds a dusty book.',inp['prompts'][0])
        self.assertIn('"empty_memory":true',inp['prompts'][0])

    def test_empty_pipeline_records_evidence_gap_and_keeps_one_final_answer(self):
        from script_core import adaptive_transition
        from script_core.temporal_pipeline import qa_context_inputs
        packet,bundle=fixture();backend=Backend()
        bundle['memory']=adaptive_transition.new_memory('v',packet['captions'],'source')
        adaptive_transition.rebuild_indexes(bundle['memory'])
        bundle['qa_input']=qa_context_inputs(dict(packet['question'],id='q'),packet['captions'],
            bundle['selected_seconds'],adaptive_transition.memory_context(bundle['memory']))
        with tempfile.TemporaryDirectory() as folder:
            result=partial_temporal.run(packet,bundle,backend,load_config('configs/script_memory_quotes.yaml'),
                folder,dict(online_seconds=1500))
        self.assertIsNone(result['error'],result['error'])
        self.assertTrue(result['qa_evidence']['empty_memory'])
        self.assertTrue(any(row['stage']=='final_qa_evidence' for row in result['incomplete']))
        self.assertEqual(sum(op=='videotree_qa' for op,_ in backend.calls),1)

    def test_cli_rejects_switch_that_would_ignore_memory_input_mode(self):
        import script_qa
        from unittest.mock import patch
        with patch.object(script_qa,'preflight',side_effect=AssertionError('must reject before model setup')):
            with self.assertRaisesRegex(ValueError,'--scheme4 cannot'):
                script_qa.main(['query','--base','unused','--questions','unused','--out','unused',
                    '--config','configs/script_memory_quotes.yaml','--scheme4'])

    def test_opt_in_configuration_rejects_ignored_mode_and_invalid_limits(self):
        self.assertEqual(load_config()['query']['temporal_qa_evidence'],'lazy_script')
        cfg=load_config('configs/script_memory_quotes.yaml')
        for key,value in [('pipeline','scheme4'),('temporal_qa_max_quotes',0),
                          ('temporal_qa_quotes_per_fact',True)]:
            bad=deepcopy(cfg);bad['query'][key]=value
            with self.assertRaises(ValueError):validate_config(bad)


if __name__=='__main__':unittest.main()
