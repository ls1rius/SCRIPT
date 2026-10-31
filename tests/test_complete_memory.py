"""Sparse query memory must never gate storage or turn actions into states."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest

from script_core.complete_memory import source_bundle,coverage_index,compact_view,final_input,quote_positions
from script_core.temporal_pipeline import qa_context_inputs
from script_core.normalized_memory import normalize
from script_core.config import load_config
from script.run_complete_memory_comparison import answer_once
from tests.test_normalized_memory import Meter,fact
from tests.test_partial_temporal import fixture


class CompleteMemoryTests(unittest.TestCase):
    def setUp(self):
        self.packet,_=fixture()
        self.view=dict(source_type='caption',entities=[],states=[],relations=[],transitions=[],identities=[],lineage=[],
            context_selection={},evidence_policy='unknown fields remain unknown')
        self.context=dict(context=self.view,selected_seconds=list(range(6)))
        self.original=qa_context_inputs(dict(self.packet['question'],id='q'),self.packet['captions'],list(range(6)),self.view)

    def test_empty_entity_memory_still_materializes_every_read_caption(self):
        bundle=source_bundle(self.packet,self.context,[])
        seen={c for u in bundle['units'] for c in u['source_caption_ids']}
        self.assertEqual(seen,{c['id'] for c in self.packet['captions']})
        self.assertEqual(len(bundle['read_caption_ids']),6)
        self.assertNotIn('temporal_slots',bundle['structural_memory'])

    def test_unread_sources_are_archived_but_do_not_enter_materialization(self):
        self.context['selected_seconds']=[0,1]
        bundle=source_bundle(self.packet,self.context,[])
        self.assertNotIn('A wall is white.',str(bundle['units']))
        self.assertEqual(bundle['read_caption_ids'],['C0','C1'])

    def test_empty_read_scope_and_duplicate_original_ids_are_not_reported_as_complete(self):
        context=deepcopy(self.context);context['selected_seconds']=[]
        with self.assertRaises(ValueError):source_bundle(self.packet,context,[])
        packet=deepcopy(self.packet);packet['captions'][1]['id']='C0'
        with self.assertRaises(ValueError):source_bundle(packet,self.context,[])

    def materialize_with_residuals(self):
        bundle=source_bundle(self.packet,self.context,[])
        # Empty normalizer facts must preserve all text, rather than produce an
        # empty memory that was previously accepted as a sufficient answer view.
        count=(len(bundle['units'])+3)//4
        normalized,audit=normalize(bundle,Meter([dict(facts=[],retained=[]) for _ in range(count)]))
        return bundle,normalized

    def test_coverage_requires_real_facts_or_visible_residual_for_each_source(self):
        bundle,normalized=self.materialize_with_residuals()
        coverage=coverage_index(self.packet,bundle,normalized)
        self.assertEqual(coverage['coverage'],1.)
        self.assertEqual(coverage['covered_captions'],6)
        normalized['retained_evidence']={}
        with self.assertRaisesRegex(ValueError,'neither facts nor residual'):
            coverage_index(self.packet,bundle,normalized)

    def test_repeated_action_text_keeps_distinct_occurrence_times(self):
        packet=deepcopy(self.packet);packet['captions'][1]['text']=packet['captions'][0]['text']
        bundle=source_bundle(packet,self.context,[])
        row=next(u for u in bundle['units'] if 'C0' in u['source_caption_ids'])
        self.assertIn('C1',row['source_caption_ids'])
        self.assertIn([0,1],row['source_intervals']);self.assertIn([1,2],row['source_intervals'])
        self.assertNotIn([0,2],row['source_intervals'])

    def test_old_merged_state_does_not_replace_later_action_or_create_continuous_span(self):
        self.context['selected_seconds']=[0,1]
        self.view['states']=[dict(id='T0',entity_id='E0',description=self.packet['captions'][0]['text'],
            source_caption_ids=['C0','C1'],observed_support_interval=[0,2])]
        bundle=source_bundle(self.packet,self.context,[])
        self.assertTrue(any(u['text']==self.packet['captions'][1]['text'] for u in bundle['units']))
        self.assertTrue(all([0,2] not in u['source_intervals'] for u in bundle['units']))

    def test_original_system_question_option_order_and_protocol_are_unchanged(self):
        bundle,normalized=self.materialize_with_residuals()
        coverage=coverage_index(self.packet,bundle,normalized);packed=compact_view(bundle,normalized,coverage)
        for quotes in (True,False):
            result=final_input(self.original,self.packet,packed,[],quotes)
            self.assertEqual(result['head'],self.original['head'])
            self.assertIn('Questions: What does Alice do?',result['prompts'][0])
            self.assertIn('A wall is white.',result['prompts'][0])
        self.assertEqual(packed['coverage']['read_captions'],6)
        self.assertNotIn('temporal_slots',packed)

    def test_compaction_preserves_categories_and_evidence_instead_of_source_prefixes_only(self):
        bundle,normalized=self.materialize_with_residuals()
        packed=compact_view(bundle,normalized,coverage_index(self.packet,bundle,normalized))
        texts={r[2] for r in packed['categorized_originals']}
        self.assertTrue(all(c['text'] in texts for c in self.packet['captions']))
        self.assertEqual(packed['source_type'],'caption')
        self.assertEqual(packed['temporal_caption_order'],[f'C{i}' for i in range(6)])

    def test_quotes_are_auxiliary_and_time_covered(self):
        self.assertEqual(quote_positions(self.packet,list(range(180)),3),[0,90,179])
        self.assertEqual(len(quote_positions(self.packet,list(range(180)),1)),1)
        with self.assertRaises(ValueError):quote_positions(self.packet,[0,1],0)

    def test_legacy_baseline_parser_is_not_silently_replaced_by_strict_protocol(self):
        class Backend:
            def call(self,op,inputs,timeout):
                if op=='temporal_qa_check':return dict(input_tokens=100)
                if op=='videotree_qa':return dict(data=dict(selected_option='2',text='prediction: C walks away.'))
                raise AssertionError(op)
        with tempfile.TemporaryDirectory() as folder:
            result=answer_once(Path(folder),'q','original_replay',self.original,Backend(),load_config())
        self.assertEqual(result['selected_option'],'2')
        self.assertIsNone(result['strict_audit_option'])
        self.assertIsNone(result['error'])

    def test_cli_repair_materializes_unindexed_captions_before_final_answer(self):
        from tests.test_partial_temporal import Backend,fixture
        from script_core import partial_temporal
        class CompleteBackend(Backend):
            def call(self,op,inp,timeout):
                if op=='normalize_memory_units':
                    self.calls.append((op,deepcopy(inp)))
                    return dict(data=dict(facts=[],retained=[]))
                return super().call(op,inp,timeout)
        for config in ['configs/script_complete_memory.yaml','configs/script_complete_structured.yaml']:
            packet,bundle=fixture();backend=CompleteBackend()
            with tempfile.TemporaryDirectory() as folder:
                result=partial_temporal.run(packet,bundle,backend,load_config(config),folder,dict(online_seconds=1500))
                archive=Path(folder)/'source_archive.json'
                self.assertTrue(archive.exists())
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['source_coverage']['read_captions'],6)
            self.assertEqual(result['source_coverage']['covered_captions'],6)
            self.assertEqual(sum(op=='videotree_qa' for op,_ in backend.calls),1)
            qa=next(inp for op,inp in backend.calls if op=='videotree_qa')
            self.assertIn('A wall is white.',qa['prompts'][0])
            self.assertEqual(qa['head'],bundle['qa_input']['head'])


if __name__=='__main__':unittest.main()
