from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from script_core.semantic_memory import (accept_batch, binding_supported,
    clean_unit, normalize, output_schema, validate_coverage, view, COLLECTIONS, ROLES)
from script_core.schemas import validate_output
from tests.test_normalized_memory import unit


def action(**roles):
    return dict(kind='action',source_id='U0',actor='C',verb='wipes',**roles)


def batch(text,facts,residuals=()):
    return accept_batch([clean_unit(unit(text))],report(facts,residuals))


def report(facts=(),residuals=()):
    result={v:[] for v in COLLECTIONS.values()}
    for f in facts:
        result[COLLECTIONS[f['kind']]].append({k:v for k,v in f.items() if k!='kind'})
    result['residuals']=list(residuals)
    return result


class Meter:
    def __init__(self,reports): self.reports=iter(reports);self.calls=[];self.fixture=reports[0] if reports else None
    def json(self,op,inp,required,**kwargs):
        self.calls.append((op,deepcopy(inp),kwargs))
        if op=='semantic_memory_extract' and self.fixture and 'actions' in self.fixture:
            ids={u['id'] for u in inp['units']}
            facts=[deepcopy(f) for f in self.fixture[COLLECTIONS[inp['category']]] if f['source_id'] in ids]
            if inp['category']=='action':
                for f in facts:
                    for role in ROLES:f.setdefault(role,None)
            return dict(facts=facts,
                uncertain_source_ids=[]),dict(input_tokens=50)
        return next(self.reports),dict(input_tokens=50)


class SemanticMemoryTests(unittest.TestCase):
    def test_source_tag_is_metadata_and_extraction_gets_one_actor_c(self):
        source=unit('#C C wipes the book with a cloth.')
        meter=Meter([report([action(patient='book',instrument='cloth')])])
        memory,audit=normalize(dict(units=[source],read_caption_ids=['C0']),meter)
        self.assertEqual(meter.calls[0][1]['units'],[dict(id='U0',text='C wipes the book with a cloth.')])
        self.assertEqual(memory['sources'][0]['source_marker'],'#C')
        self.assertEqual(memory['sources'][0]['raw_text'],source['text'])
        self.assertEqual(memory['actions'][0]['actor'],'C')
        self.assertNotIn('source_location', memory['actions'][0])
        self.assertFalse(any(v is None for v in memory['actions'][0].values()))
        self.assertEqual(memory['residuals'],[])
        self.assertEqual(audit['coverage']['covered_captions'],1)
        self.assertEqual({c[1]['category'] for c in meter.calls},set(COLLECTIONS))
        self.assertTrue(all(c['checked_units']==1 for c in audit['category_coverage'].values()))

    def test_extraction_reviews_all_roles_but_stored_action_schema_is_sparse(self):
        extraction=output_schema('semantic_memory_extract',dict(category='action',units=[unit()]))
        item=extraction['properties']['facts']['items']
        self.assertTrue(set(ROLES)<=set(item['required']))
        for role in ROLES:
            self.assertEqual(item['properties'][role]['type'],['string','null'])
        stored=output_schema('semantic_memory_extract',dict(units=[unit()]))
        item=stored['properties']['actions']['items']
        self.assertFalse(set(ROLES)&set(item['required']))

    def test_one_sentence_can_have_action_and_attribute_and_multiple_residual_tags(self):
        facts=[action(patient='red book',instrument='cloth'),
            dict(kind='attribute',source_id='U0',entity='book',property='color',value='red')]
        report=batch('C wipes the red book with a cloth.',facts)
        self.assertEqual([f['kind'] for f in report['facts']],['action','attribute'])
        self.assertEqual(report['residuals'][0]['information_types'],['action','attribute'])

    def test_surface_and_instrument_are_distinct_from_patient(self):
        fact=action(surface='drawing book',instrument='pencil');fact['verb']='draws'
        report=batch('C draws on the drawing book with a pencil.',[fact])
        self.assertEqual(report['facts'],[fact]);self.assertEqual(report['residuals'],[])
        bad=dict(fact,patient='with a pencil');bad.pop('surface')
        report=batch('C draws on the drawing book with a pencil.',[bad])
        self.assertEqual(report['facts'],[])
        self.assertTrue(report['residuals'])

    def test_whole_clause_in_verb_cannot_hide_missing_roles(self):
        fact=action();fact['verb']='wipes the book'
        report=batch('C wipes the book with a cloth.',[fact])
        self.assertEqual(report['facts'],[])
        self.assertEqual(report['rejected'][0]['reason'],'verb_contains_arguments_or_unsupported_syntax')

    def test_copula_is_not_an_action_and_body_part_is_not_a_manner(self):
        self.assertEqual(batch('The book is wet.',[dict(kind='action',source_id='U0',actor='book',verb='is',patient='wet')])['facts'],[])
        fact=action(patient='book',manner='left hand');fact['verb']='moves'
        self.assertEqual(batch('C moves the book with her left hand.',[fact])['facts'],[])
        good=action(patient='book',manner='carefully')
        self.assertEqual(batch('C carefully wipes the book.',[good])['facts'],[good])

    def test_origin_destination_material_and_body_part_preserve_their_roles(self):
        fact=action(patient='book',source_location='shelf',destination='bag',body_part='left hand');fact['verb']='moves'
        report=batch('C moves the book from the shelf into the bag with her left hand.',[fact])
        self.assertEqual(report['facts'],[fact]);self.assertTrue(report['residuals'])
        bad=dict(fact,destination='shelf')
        self.assertEqual(batch('C moves the book from the shelf into the bag with her left hand.',[bad])['facts'],[])

    def test_negation_and_uncertainty_are_not_removed_by_word_coverage(self):
        for text in ['C never wipes the book.','C might wipe the book.']:
            fact=action(patient='book');fact['verb']='wipe' if 'might' in text else 'wipes'
            report=batch(text,[fact])
            self.assertEqual(report['facts'],[])
            self.assertEqual(report['residuals'][0]['text'],text)

    def test_state_and_stable_property_use_different_schemas(self):
        state=dict(kind='state',source_id='U0',entity='book',dimension='wetness',value='wet')
        self.assertEqual(batch('The book is wet.',[state])['facts'],[state])
        prop=dict(kind='attribute',source_id='U0',entity='book',property='color',value='red')
        self.assertEqual(batch('The red book is wet.',[prop])['facts'],[prop])
        invalid=dict(prop,property='condition')
        with self.assertRaises(ValueError):batch('The book is wet.',[invalid])

    def test_attribute_cannot_move_to_another_object(self):
        prop=dict(kind='attribute',source_id='U0',entity='book',property='color',value='black')
        self.assertEqual(batch('C moves the red book with a black cloth.',[prop])['facts'],[])

    def test_explicit_change_is_not_invented_from_an_action(self):
        change=dict(kind='change',source_id='U0',entity='book',before='wet',after='dry')
        self.assertEqual(batch('The book changes from wet to dry.',[change])['facts'],[change])
        self.assertEqual(batch('C dries the book.',[change])['facts'],[])

    def test_temporal_order_does_not_authorize_a_causal_link(self):
        link=dict(kind='event_link',source_id='U0',left_event='C wipes the book',connector='then',
            right_event='C moves the book',link_type='before')
        self.assertEqual(batch('C wipes the book then C moves the book.',[link])['facts'],[link])
        self.assertEqual(batch('C wipes the book then C moves the book.',[dict(link,link_type='cause')])['facts'],[])

    def test_actions_and_movement_are_not_attachment_or_part_whole_relations(self):
        for text,left,predicate,right,subtype in [
            ('C wipes the red book.','C','wipes','red book','attachment'),
            ('C draws on the drawing book.','C','draws','drawing book','attachment'),
            ('C moves the book from the shelf into the bag.','book','from','shelf','part_whole'),
            ('C moves the book from the shelf into the bag.','book','into','bag','part_whole')]:
            with self.subTest(predicate=predicate):
                fact=dict(kind='relation',source_id='U0',left=left,predicate=predicate,right=right,relation_type=subtype)
                result=batch(text,[fact])
                self.assertEqual(result['facts'],[])
                self.assertEqual(result['rejected'][0]['reason'],'unsupported_relation_subtype_binding')
                self.assertEqual(result['residuals'][0]['text'],text)

    def test_supported_relation_subtypes_keep_their_literal_predicate(self):
        for text,left,predicate,right,subtype in [
            ('The book is on the table.','book','is on','table','spatial'),
            ('The strap is attached to the bag.','strap','is attached to','bag','attachment'),
            ('The cover is part of the book.','cover','is part of','book','part_whole'),
            ('C owns the book.','C','owns','book','possession')]:
            with self.subTest(subtype=subtype):
                fact=dict(kind='relation',source_id='U0',left=left,predicate=predicate,right=right,relation_type=subtype)
                self.assertEqual(batch(text,[fact])['facts'],[fact])

    def test_residual_is_kept_when_connectors_or_possessives_are_unrepresented(self):
        fact=action(patient='book',instrument='cloth')
        report=batch('C wipes his book with a cloth.',[fact])
        self.assertEqual(report['facts'],[fact])
        self.assertEqual(report['residuals'][0]['text'],'C wipes his book with a cloth.')

    def test_view_keeps_discrete_times_and_multicategory_overlap_for_ablation(self):
        source=unit('#C C wipes the red book with a cloth.');source['source_intervals']=[[1,2],[10,11]]
        meter=Meter([report([action(patient='red book',instrument='cloth'),
            dict(kind='attribute',source_id='U0',entity='book',property='color',value='red')])])
        memory,_=normalize(dict(units=[source],read_caption_ids=['C0']),meter)
        rendered=view(memory,included=['action'])
        row=rendered['observations'][0]
        self.assertEqual(row['sample_intervals'],[[1,2],[10,11]])
        self.assertIn('actions',row);self.assertNotIn('attributes',row)
        self.assertEqual(rendered['category_audit']['residual_sources_overlapping_excluded_categories'],['U0'])
        self.assertNotIn('source_fields',rendered)

    def test_every_read_caption_requires_facts_or_visible_residual(self):
        source=unit();bundle=dict(units=[source],read_caption_ids=['C0'])
        memory,_=normalize(bundle,Meter([report()]))
        self.assertEqual(memory['residuals'][0]['information_types'],['unclassified'])
        memory['residuals']=[]
        with self.assertRaises(ValueError):validate_coverage(bundle,memory)


    def test_actual_backend_projection_preserves_option_records_and_ids(self):
        from script_core.context import model_view
        options=[dict(option_id='4',text='C draws.'),dict(option_id='0',text='C reads.')]
        projected=model_view(dict(options=options))
        self.assertEqual(projected['options'],options)
        self.assertEqual([o['option_id'] for o in projected['option_distinctions']],['4','0'])



if __name__ == '__main__':unittest.main()
