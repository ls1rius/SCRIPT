"""Compact transport cannot change fact text, IDs, temporal causes or evidence."""
from copy import deepcopy
import unittest

from script_core.temporal_codec import encode_report,decode_report,encode_schema
from script_core.temporal_entity import output_schema
from script_core.schemas import validate_output,decoder_schema


class TemporalCodecTests(unittest.TestCase):
    def test_round_trip_preserves_literals_that_look_like_short_keys(self):
        value=dict(entities=[dict(entity_ref='new',label='e',attributes=[dict(
            field='color',value='r',source_frame_ids=['f'])],observations=[dict(
            frame_id='f',state='state',continuity='initial',cause=dict(
            action=None,actor='p',tool='t',basis='unknown',source_frame_ids=[]))])],
            relations=[],more_entities=False,unresolved='e and r are literal labels')
        before=deepcopy(value)
        encoded=encode_report(value)
        self.assertEqual(encoded['e'][0]['l'],'e')
        self.assertEqual(encoded['e'][0]['a'][0]['f'],['f'])
        self.assertEqual(decode_report(encoded),before)
        self.assertEqual(value,before)

    def test_schema_and_restored_report_keep_the_same_reference_guards(self):
        inputs=dict(frames=[dict(frame_id='F0',pts=0)],entities=[],already_reported=[],page_size=4)
        schema=output_schema('temporal_observe',inputs)
        value=dict(entities=[dict(entity_ref='new',label='box',attributes=[],observations=[dict(
            frame_id='F0',state='closed',continuity='initial',cause=dict(
            action=None,actor=None,tool=None,basis='unknown',source_frame_ids=[]))])],
            relations=[],more_entities=False,unresolved='')
        validate_output(value,schema)
        validate_output(encode_report(value),encode_schema(schema))
        self.assertEqual(decode_report(encode_report(value)),value)
        bad=encode_report(value);bad['e'][0]['o'][0]['i']='F999'
        with self.assertRaises(ValueError):validate_output(bad,encode_schema(schema))
        from lmformatenforcer import JsonSchemaParser
        JsonSchemaParser(decoder_schema(encode_schema(schema)))

    def test_short_same_produces_the_identical_network_as_full_repeated_observations(self):
        from script_core.temporal_entity import new_network,ingest
        from tests.test_temporal_entity import row,report,frames
        net=new_network();ingest(net,report(row()),frames(0,3),'S0','b0',[])
        full=report(row('E0',1,'same',state='Repeated wording is not a physical change'))
        full['entities'][0]['observations'].append(row('E0',2,'same')['observations'][0])
        delta=deepcopy(full)
        delta['entities'][0]['observations']=[dict(continuity='same',frame_id=o['frame_id']) for o in full['entities'][0]['observations']]
        a,b=deepcopy(net),deepcopy(net)
        ingest(a,full,frames(0,3),'S0','b1',['E0']);ingest(b,delta,frames(0,3),'S0','b1',['E0'])
        self.assertEqual(a,b)
        self.assertEqual(a['states']['T0']['source_frame_ids'],['F0','F1','F2'])
        from script_core.temporal_entity import observe_page
        from tests.test_temporal_runtime_repairs import ScriptedMeter
        actual=deepcopy(net)
        inp=dict(frames=frames(0,3),entities=[dict(id='E0',label='box')],already_reported=[],page_size=4,
                 delta_observations=True)
        observe_page(actual,inp,ScriptedMeter(delta),'S0','b1',['E0'])
        self.assertEqual(actual,a)

    def test_short_same_schema_is_opt_in_and_cannot_encode_a_change(self):
        inp=dict(frames=[dict(frame_id='F0',pts=0)],entities=[dict(id='E0',label='box')],already_reported=[],page_size=4)
        value=dict(entities=[dict(entity_ref='E0',label='box',attributes=[],observations=[dict(continuity='same',frame_id='F0')])],relations=[],more_entities=False,unresolved='')
        with self.assertRaises(ValueError):validate_output(value,output_schema('temporal_observe',inp))
        validate_output(value,output_schema('temporal_observe',dict(inp,delta_observations=True)))
        value['entities'][0]['observations'][0]['continuity']='changed'
        with self.assertRaises(ValueError):validate_output(value,output_schema('temporal_observe',dict(inp,delta_observations=True)))

    def test_first_sighting_cannot_inherit_an_unobserved_state(self):
        from script_core.temporal_entity import new_network,observe_page
        from tests.test_temporal_runtime_repairs import ScriptedMeter
        net=new_network();before=deepcopy(net)
        value=dict(entities=[dict(entity_ref='new',label='box',attributes=[],observations=[dict(continuity='same',frame_id='F0')])],relations=[],more_entities=False,unresolved='')
        inp=dict(frames=[dict(frame_id='F0',pts=0)],entities=[],already_reported=[],page_size=4,delta_observations=True)
        with self.assertRaisesRegex(ValueError,'requires an existing scene state'):
            observe_page(net,inp,ScriptedMeter(value),'S0','b',[],max_attempts=1)
        self.assertEqual(net,before)


if __name__=='__main__':unittest.main()
