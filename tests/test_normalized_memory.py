"""Structured roles stay source-bound and residual original details stay visible."""
from copy import deepcopy
import unittest

from script_core.normalized_memory import accept_batch,evidence_units,normalize,read_prompt,literal
from script_core.budget import ToolFailure
from script.run_memory_quotes_comparison import final_inputs
from tests.test_partial_temporal import fixture
from script_core import partial_temporal


def unit(text='C wipes the book with a cloth.',id='U0',source='caption',status='caption_asserted'):
    return dict(id=id,text=text,origins=['quote:C0'],source_type=source,status=status,kind_hint='unclassified',
        source_caption_ids=['C0'] if source=='caption' else [],
        source_frame_ids=['F0'] if source=='sampled_visual' else [],source_intervals=[[0,1]])


def fact(kind='action',subject='C',predicate='wipes',object='book',value=None,tool='cloth',
         location=None,source_ids=None,attribute_field=None):
    return dict(kind=kind,attribute_field=attribute_field,subject=subject,predicate=predicate,object=object,
        value=value,tool=tool,location=location,source_ids=source_ids or ['U0'])


class Meter:
    def __init__(self,reports=None,error=None):self.reports=iter(reports or []);self.error=error;self.calls=[]
    def json(self,operation,inputs,required):
        self.calls.append((operation,deepcopy(inputs)))
        if self.error:raise self.error
        return next(self.reports),dict(call_id='normalization-call')


class NormalizedMemoryTests(unittest.TestCase):
    def test_action_roles_are_extracted_and_fully_represented_original_is_removed(self):
        source=unit();before=deepcopy(source)
        report=accept_batch([source],dict(facts=[fact()],retained=[]))
        self.assertEqual(len(report['facts']),1)
        self.assertEqual(report['facts'][0]['tool'],'cloth')
        self.assertEqual(report['retained'],[])
        self.assertEqual(source,before)

    def test_quoted_null_markers_are_normalized_before_grounding_checks(self):
        row=fact(value='null',location='null',attribute_field='null')
        out=accept_batch([unit()],dict(facts=[row],retained=[]))
        self.assertEqual(len(out['facts']),1)
        self.assertIsNone(out['facts'][0]['value'])
        self.assertIsNone(out['facts'][0]['location'])
        self.assertEqual(out['retained'],[])

    def test_missing_color_keeps_original_until_an_attribute_fact_represents_it(self):
        source=unit('C wipes the red book with a cloth.')
        out=accept_batch([source],dict(facts=[fact()],retained=[]))
        self.assertEqual(out['retained'][0]['text'],source['text'])
        self.assertIn('red',out['retained'][0]['unrepresented_tokens'])
        attr=fact(kind='attribute',subject='book',predicate=None,object=None,value='red',tool=None,attribute_field='color')
        out=accept_batch([source],dict(facts=[fact(),attr],retained=[]))
        self.assertEqual(out['retained'],[])
        self.assertEqual(out['facts'][1]['attribute_field'],'color')

    def test_missing_spatial_relation_is_not_silently_deleted(self):
        source=unit('C puts the book on the shelf.')
        action=fact(predicate='puts',tool=None,location='shelf')
        out=accept_batch([source],dict(facts=[action],retained=[]))
        self.assertIn('on',out['retained'][0]['unrepresented_tokens'])
        relation=fact(kind='relation',subject='book',predicate='on',object='shelf',tool=None)
        out=accept_batch([source],dict(facts=[action,relation],retained=[]))
        self.assertEqual(out['retained'],[])

    def test_unstated_tool_and_changed_verb_form_are_rejected(self):
        for bad in [fact(tool='sponge'),fact(predicate='wipe')]:
            out=accept_batch([unit()],dict(facts=[bad],retained=[]))
            self.assertEqual(out['facts'],[])
            self.assertEqual(len(out['retained']),1)
            self.assertEqual(len(out['rejected']),1)
        self.assertFalse(literal('C','cloth'))

    def test_unrelated_sources_cannot_be_joined_into_an_invented_action(self):
        units=[unit(),unit('A hammer lies on the desk.','U1')]
        out=accept_batch(units,dict(facts=[fact(tool='hammer',source_ids=['U0','U1'])],retained=[]))
        self.assertEqual(out['facts'],[])
        self.assertEqual(len(out['retained']),2)

    def test_negation_cannot_be_dropped_from_a_positive_atomic_fact(self):
        out=accept_batch([unit('C never wipes the book with a cloth.')],dict(facts=[fact()],retained=[]))
        self.assertEqual(out['facts'],[])
        self.assertIn('never',out['retained'][0]['text'])

    def test_reversed_roles_cannot_pass_just_because_all_words_are_present(self):
        wrong=fact(subject='book',object='C')
        out=accept_batch([unit()],dict(facts=[wrong],retained=[]))
        self.assertEqual(out['facts'],[])
        self.assertTrue(out['retained'])

    def test_an_attribute_of_a_different_object_is_not_reassigned(self):
        source=unit('C moves the red book with a black cloth.')
        wrong=fact(kind='attribute',subject='book',predicate=None,object=None,value='black',tool=None,attribute_field='color')
        out=accept_batch([source],dict(facts=[wrong],retained=[]))
        self.assertEqual(out['facts'],[])
        self.assertEqual(out['retained'][0]['text'],source['text'])

    def test_inferred_and_visual_provenance_are_preserved(self):
        for source,status in [('text_inference','text_inferred'),('sampled_visual','accepted_upstream_visual_model_observation')]:
            out=accept_batch([unit(source=source,status=status)],dict(facts=[fact()],retained=[]))
            self.assertEqual(out['facts'][0]['source_type'],[source])
            self.assertEqual(out['facts'][0]['epistemic_status'],[status])

    def test_normalizer_sees_only_units_not_question_or_options(self):
        meter=Meter([dict(facts=[fact()],retained=[])])
        bundle=dict(units=[unit()],structural_memory=dict(entities=[]))
        normalized,audit=normalize(bundle,meter)
        self.assertEqual(meter.calls[0][0],'normalize_memory_units')
        self.assertEqual(set(meter.calls[0][1]),{'units','source_type'})
        self.assertEqual(audit['raw_text_removed_units'],1)
        self.assertEqual(normalized['actions'][0]['subject'],'C')
        self.assertTrue(audit['all_source_units_accounted_for'])

    def test_invalid_normalization_retains_categorized_sources_and_records_failure(self):
        meter=Meter(error=ToolFailure('Invalid structured JSON'))
        normalized,audit=normalize(dict(units=[unit()],structural_memory={}),meter)
        self.assertEqual(audit['normalized_facts'],0)
        self.assertEqual(audit['retained_units'],1)
        self.assertTrue(audit['batch_failures'])
        self.assertEqual(normalized['retained_evidence']['unclassified'][0]['text'],unit()['text'])

    def test_cuda_failures_propagate_instead_of_producing_fake_success(self):
        with self.assertRaises(ToolFailure):
            normalize(dict(units=[unit()],structural_memory={}),Meter(error=ToolFailure('CUDA out of memory')))

    def test_saved_scheme_one_prompt_is_the_only_source_of_units(self):
        packet,bundle=fixture();memory=partial_temporal.project(bundle['memory'])
        context=dict(context=partial_temporal.context(memory,bundle['memory'],dict(question='',options=[])),
            selected_seconds=bundle['selected_seconds'])
        pair,_=final_inputs(packet,context,[])
        parts=read_prompt(pair['memory_quotes'])
        source=evidence_units(pair['memory_quotes'])
        self.assertEqual(source['qa_policy'],parts['policy'])
        self.assertTrue(source['units'])
        self.assertNotIn('A wall is white.',str(source))
        self.assertNotIn('question',source)
        self.assertNotIn('options',source)
        self.assertTrue(all('description' not in s for s in source['structural_memory']['temporal_slots']))


if __name__=='__main__':unittest.main()
