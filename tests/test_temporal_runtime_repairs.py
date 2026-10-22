"""Regressions for the 2026-10-20 GPU observation failures."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from script_core.aligned_pipeline import extract_scene
from script_core.budget import BudgetLedger, Meter
from script_core.config import load_config
from script_core.schemas import decoder_schema, validate_output
from script_core.temporal_entity import (new_network, ingest, observe_page, output_schema,
                                        compact_profiles,collapse_exact_known_rows,align,align_page)


def entity(ref='new', label='box', t=0, continuity='initial'):
    return dict(entity_ref=ref, label=label, attributes=[], observations=[dict(
        frame_id=f'F{t}', state='closed red box', continuity=continuity,
        cause=dict(action=None, actor=None, tool=None, basis='unknown', source_frame_ids=[]))])


def page(*entities, more=False, relations=()):
    return dict(entities=list(entities), relations=list(relations), more_entities=more, unresolved='')


class ScriptedMeter:
    def __init__(self, *reports):
        self.reports=list(reports)
        self.requests=[]

    def json(self, op, inputs, required, **kwargs):
        self.requests.append(deepcopy(inputs))
        return deepcopy(self.reports.pop(0)), dict(call_id=str(len(self.requests)))


class RuntimeRepairTests(unittest.TestCase):
    def setUp(self):
        self.frames=[dict(frame_id=f'F{i}', pts=float(i)) for i in range(3)]
        self.net=new_network()
        ingest(self.net, page(entity()), self.frames, 'S0', 'seed', [])
        self.inputs=dict(frames=self.frames, entities=[dict(id='E0')],
                         already_reported=[], page_size=4)

    def test_distinct_objects_reusing_one_identity_are_reobserved_atomically(self):
        bad=page(entity('E0','television',1,'same'), entity('E0','sofa',1,'same'))
        good=page(entity(label='television',t=1))
        meter=ScriptedMeter(bad,good)
        ids,_,_=observe_page(self.net,self.inputs,meter,'S0','batch',['E0'])
        self.assertEqual(ids,['E1'])
        self.assertEqual(self.net['next_entity'],2)
        self.assertEqual(self.net['entities']['E0']['label'],'box')
        self.assertEqual(len(self.net['entities']['E0']['state_ids']),1)
        self.assertIn('duplicate',meter.requests[1]['validation_feedback']['error'])
        self.assertEqual(len(self.net['observation_repairs']),1)
        self.assertEqual(meter.requests[1]['page_size'],1)
        self.assertTrue(self.net['observation_page_recoveries'][0]['continuation_required'])

    def test_literal_self_relation_is_excluded_by_schema(self):
        schema=output_schema('temporal_observe',self.inputs)
        bad=page(entity('E0',t=1,continuity='same'),relations=[dict(
            subject='E0',object='E0',predicate='sews',frame_id='F1',
            update='present',source_frame_ids=['F1'])])
        with self.assertRaises(ValueError):validate_output(bad,schema)
        from lmformatenforcer import JsonSchemaParser
        JsonSchemaParser(decoder_schema(schema))

    def test_alias_self_relation_retries_without_discarding_evidence(self):
        rel=dict(subject='B0',object='E0',predicate='sews',frame_id='F1',
                 update='present',source_frame_ids=['F1'])
        bad=page(entity('E0',t=1,continuity='same'),relations=[rel])
        good=page(entity('E0',t=1,continuity='same'),entity(label='fabric',t=1),
                  relations=[dict(rel,object='B1')])
        meter=ScriptedMeter(bad,good)
        observe_page(self.net,self.inputs,meter,'S0','batch',['E0'])
        self.assertEqual(len(self.net['relations']),1)
        self.assertEqual((self.net['relations'][0]['subject'],self.net['relations'][0]['object']),('E0','E1'))
        self.assertEqual(self.net['relations'][0]['source_frame_ids'],['F1'])

    def test_reported_inventory_ids_are_endpoints_but_not_new_page_rows(self):
        inp=dict(self.inputs,already_reported=[dict(id='E0')])
        schema=output_schema('temporal_observe',inp)
        with self.assertRaises(ValueError):validate_output(page(entity('E0')),schema)
        rel=dict(subject='E0',object='B0',predicate='contains',frame_id='F1',
                 update='present',source_frame_ids=['F1'])
        validate_output(page(entity(label='tool',t=1),relations=[rel]),schema)

    def test_exhausted_semantic_retries_leave_graph_unchanged_and_raise(self):
        before=deepcopy(self.net)
        bad=page(entity('E0',t=1,continuity='same'),entity('E0',label='another object',t=1,continuity='same'))
        with self.assertRaisesRegex(ValueError,'failed after 3 attempts'):
            observe_page(self.net,self.inputs,ScriptedMeter(bad,bad,bad),'S0','batch',['E0'])
        self.assertEqual(self.net,before)

    def test_existing_entity_update_counts_as_batch_pagination_progress(self):
        # GPU failure: batch 2/page 0 updates E0 and says more=true. The old code
        # compared the global inventory size, then failed before reading page 1.
        reports=[page(entity(),more=False),
                 page(entity('E0',t=1,continuity='same'),more=True),
                 page(entity(label='tool',t=1))]
        class Backend:
            def __init__(self):self.inputs=[]
            def call(self,op,inp,timeout):
                self.inputs.append(deepcopy(inp))
                return dict(data=reports.pop(0))
        backend=Backend()
        cfg=load_config()
        with tempfile.TemporaryDirectory() as tmp:
            ledger=BudgetLedger(600)
            meter=Meter(backend,ledger,cfg,Path(tmp)/'calls')
            try:
                net=new_network()
                ids=extract_scene(net,dict(id='S0',interval=[0,2]),self.frames[:2],meter,tmp,4,1)
                self.assertEqual(ids,['E0','E1'])
                self.assertEqual([r['page'] for r in backend.inputs],[0,0,1])
                self.assertEqual(backend.inputs[2]['already_reported'][0]['id'],'E0')
                self.assertEqual(len(net['coverage']),3)
            finally:meter.close()

    def test_all_semantic_attempts_are_charged_to_the_same_budget(self):
        bad=page(entity('E0',t=1,continuity='same'),entity('E0',label='another object',t=1,continuity='same'))
        reports=[bad,page(entity('E0',t=1,continuity='same'))]
        class Backend:
            def call(self,op,inp,timeout):return dict(data=reports.pop(0))
        with tempfile.TemporaryDirectory() as tmp:
            ledger=BudgetLedger(600)
            meter=Meter(Backend(),ledger,load_config(),Path(tmp)/'calls')
            try:
                observe_page(self.net,self.inputs,meter,'S0','batch',['E0'])
                self.assertEqual(sum(c['operation']=='temporal_observe' for c in ledger.calls),2)
                self.assertFalse(ledger.budget_violation)
            finally:meter.close()

    def test_exact_known_repeats_keep_one_update_and_remap_all_batch_endpoints(self):
        known=entity('E0',t=1,continuity='same')
        relation=dict(subject='B1',object='B2',predicate='holds',frame_id='F1',
                      update='present',source_frame_ids=['F1'])
        raw=page(known,deepcopy(known),entity(label='tool',t=1),relations=[relation])
        meter=ScriptedMeter(raw)
        ids,_,_=observe_page(self.net,self.inputs,meter,'S0','batch',['E0'])
        self.assertEqual(ids,['E0','E1'])
        self.assertEqual((self.net['relations'][0]['subject'],self.net['relations'][0]['object']),('E0','E1'))
        self.assertEqual(self.net['states']['T0']['source_frame_ids'],['F0','F1'])
        self.assertEqual(len(self.net['observation_normalizations']),1)
        self.assertEqual(len(raw['entities']),3)

    def test_exact_new_rows_remain_separate_unconfirmed_instances(self):
        value=entity(t=1)
        result,duplicates=collapse_exact_known_rows(page(value,deepcopy(value)),['E0'])
        self.assertEqual(len(result['entities']),2)
        self.assertEqual(duplicates,[])

    def test_duplicate_aliases_cannot_hide_self_relations(self):
        value=entity('E0',t=1,continuity='same')
        rel=dict(subject='B0',object='B1',predicate='holds',frame_id='F1',
                 update='present',source_frame_ids=['F1'])
        before=deepcopy(self.net)
        with self.assertRaisesRegex(ValueError,'collapsed distinct endpoints'):
            observe_page(self.net,self.inputs,ScriptedMeter(page(value,deepcopy(value),relations=[rel])),
                         'S0','batch',['E0'],max_attempts=1)
        self.assertEqual(self.net,before)

    def test_profiles_factor_repeated_attributes_without_losing_sources(self):
        attrs=[dict(field='color',value='red',source_frame_ids=['F0'],observed_at=[0.],batch_id='b0'),
               dict(field='color',value='red',source_frame_ids=['F1'],observed_at=[1.],batch_id='b1'),
               dict(field='color',value='blue',source_frame_ids=['F2'],observed_at=[2.],batch_id='b2')]
        rows=[dict(id='E0',label='box',attributes=attrs,last_state=deepcopy(self.net['states']['T0']))]
        before=deepcopy(rows);projected=compact_profiles(rows)
        self.assertEqual(len(projected[0]['attributes']),2)
        self.assertEqual(projected[0]['attributes'][0]['source_frame_ids'],['F0','F1'])
        self.assertEqual(projected[0]['attributes'][0]['observed_at'],[0.,1.])
        self.assertEqual(rows,before)
        self.assertEqual(compact_profiles(projected),projected)

    def test_already_reported_input_does_not_duplicate_complete_profiles(self):
        profile=dict(id='E0',label='box',attributes=[],last_state=deepcopy(self.net['states']['T0']))
        inp=dict(self.inputs,entities=[profile],already_reported=[profile])
        meter=ScriptedMeter(page(entity(label='tool',t=1)))
        observe_page(self.net,inp,meter,'S0','batch',['E0'])
        self.assertEqual(meter.requests[0]['already_reported'],[dict(id='E0')])

    def test_real_gpu_duplicate_page_is_normalized_without_inventing_entities(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/temporal_r2_runtime.json').read_text())
        raw=fixture['identical_known_rows']['output']
        normalized,duplicates=collapse_exact_known_rows(raw,['E12'])
        self.assertEqual(len(normalized['entities']),1)
        self.assertEqual(len(duplicates),3)
        self.assertEqual(normalized['entities'][0],raw['entities'][0])
        self.assertEqual(normalized['entities'][0]['entity_ref'],'E12')

    def test_real_overflow_profile_projection_retains_every_selected_attribute_source(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/temporal_r2_runtime.json').read_text())
        original=fixture['overflow']['inputs']['entities']
        projected=compact_profiles(original)
        def facts(rows):
            return {(row['id'],a['field'],a['value'],f) for row in rows
                    for a in row['attributes'] for f in a['source_frame_ids']}
        self.assertEqual(facts(original),facts(projected))
        self.assertLess(len(json.dumps(projected)),len(json.dumps(original)))

    def test_known_id_label_binding_rejects_fabric_assigned_to_needle(self):
        inp=dict(self.inputs,entities=[dict(id='E0',label='needle')])
        schema=output_schema('temporal_observe',inp)
        with self.assertRaises(ValueError):validate_output(page(entity('E0','fabric',1,'same')),schema)
        validate_output(page(entity('E0','needle',1,'same')),schema)
        validate_output(page(entity('new','fabric',1)),schema)
        from lmformatenforcer import JsonSchemaParser
        JsonSchemaParser(decoder_schema(schema))

    def alignment_inputs(self):
        net=new_network()
        ingest(net,page(entity()),self.frames,'S0','b0',[])
        ingest(net,page(entity(t=2)),self.frames,'S1','b1',[])
        inp=dict(frames=self.frames,pairs=[['E1','E0']],
            left_entities=[dict(id='E1',state_ids=['T1'])],right_entities=[dict(id='E0',state_ids=['T0'])],
            states=list(net['states'].values()),compare_state_pairs=[dict(left='E1',right='E0',left_state='T1',right_state='T0')])
        return net,inp

    def match(self,decision='same',refs=None):
        return dict(left='E1',right='E0',left_state='T1',right_state='T0',decision=decision,
            state_continuity='unknown',reason='fixture',source_frame_ids=['F0','F2'] if refs is None else refs,
            ended_relation_ids=[])

    def test_unknown_identity_with_one_sided_or_empty_evidence_preserves_history(self):
        for refs in [[],['F0']]:
            net,inp=self.alignment_inputs();before=deepcopy(net)
            align(net,dict(matches=[self.match('unknown',refs)]),self.frames,inp['pairs'])
            for field in ['entities','states','relations','transitions','aliases','lineage']:
                self.assertEqual(net[field],before[field])
            self.assertEqual(net['identities'][0]['status'],'unresolved_identity_comparison')

    def test_unknown_cannot_assert_continuity_end_relations_or_foreign_sources(self):
        for fields in [dict(state_continuity='same'),dict(ended_relation_ids=['R0']),dict(source_frame_ids=['F999'])]:
            net,inp=self.alignment_inputs()
            with self.assertRaises(ValueError):
                align(net,dict(matches=[dict(self.match('unknown',[]),**fields)]),self.frames,inp['pairs'])

    def test_supported_identity_still_requires_both_sides(self):
        net,inp=self.alignment_inputs();before=deepcopy(net)
        with self.assertRaisesRegex(ValueError,'both entities'):
            align(net,dict(matches=[self.match(refs=['F0'])]),self.frames,inp['pairs'])
        self.assertEqual(net,before)

    def test_alignment_schema_binds_each_pair_to_its_own_source_allowlists(self):
        net,inp=self.alignment_inputs()
        meter=ScriptedMeter(dict(matches=[self.match('unknown',[])]))
        align_page(net,inp,meter)
        schema=output_schema('temporal_align',meter.requests[0])
        good=dict(self.match(refs=[]),left_source_frame_id='F2',right_source_frame_id='F0')
        validate_output(dict(matches=[good]),schema)
        with self.assertRaises(ValueError):validate_output(dict(matches=[dict(good,left_source_frame_id='F0')]),schema)
        with self.assertRaises(ValueError):validate_output(dict(matches=[self.match(refs=['F0'])]),schema)
        validate_output(dict(matches=[self.match('unknown',[])]),schema)
        omitted=self.match('unknown',[]);omitted.pop('ended_relation_ids')
        validate_output(dict(matches=[omitted]),schema)
        from lmformatenforcer import JsonSchemaParser
        JsonSchemaParser(decoder_schema(schema))

    def test_alignment_validation_retries_atomically_before_state_transfer(self):
        net,inp=self.alignment_inputs()
        bad=self.match(refs=['F0']);good=dict(self.match(refs=[]),left_source_frame_id='F2',right_source_frame_id='F0')
        meter=ScriptedMeter(dict(matches=[bad]),dict(matches=[good]))
        align_page(net,inp,meter)
        self.assertEqual(len(net['entities']),1)
        self.assertEqual(len(net['alignment_repairs']),1)
        self.assertIn('validation_feedback',meter.requests[1])

    def test_recovery_page_forces_a_following_inventory_check(self):
        bad=page(entity('E0','box',1,'same'),entity('E0','other',1,'same'))
        meter=ScriptedMeter(bad,page(entity('E0','box',1,'same')))
        _,report,_=observe_page(self.net,self.inputs,meter,'S0','b1',['E0'])
        self.assertTrue(report['more_entities'])
        self.assertFalse(self.net['observation_page_recoveries'][0]['model_more_entities'])

    def test_real_r3_different_object_cannot_reuse_existing_needle_id(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/temporal_r3_runtime.json').read_text())['wrong_entity_label']
        with self.assertRaises(ValueError):
            validate_output(fixture['output'],output_schema('temporal_observe',fixture['inputs']))

    def test_real_r3_unknown_comparisons_are_retained_without_transfer(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/temporal_r3_runtime.json').read_text())['unknown_identity']
        inp=fixture['inputs'];net=new_network()
        net['states']={s['id']:deepcopy(s) for s in inp['states']}
        net['entities']={e['id']:deepcopy(e) for e in inp['left_entities']+inp['right_entities']}
        before=deepcopy(net)
        report,_=align_page(net,inp,ScriptedMeter(fixture['output']))
        self.assertEqual(len(report['matches']),4)
        self.assertEqual(len(net['identities']),4)
        for field in ['entities','states','relations','transitions','lineage','aliases']:
            self.assertEqual(net[field],before[field])

    def test_competing_same_matches_remain_unknown_without_losing_the_query(self):
        net,inp=self.alignment_inputs()
        ingest(net,page(entity(t=1)),self.frames,'S2','b2',[])
        inp.update(pairs=[['E1','E0'],['E1','E2']],states=list(net['states'].values()),
            right_entities=[dict(id='E0',state_ids=['T0']),dict(id='E2',state_ids=['T2'])],
            compare_state_pairs=[dict(left='E1',right=e,left_state='T1',right_state=s)
                                 for e,s in [('E0','T0'),('E2','T2')]])
        matches=[self.match(),dict(self.match(),right='E2',right_state='T2',source_frame_ids=['F1','F2'])]
        before=deepcopy(net)
        report,_=align_page(net,inp,ScriptedMeter(dict(matches=matches)))
        self.assertEqual([r['decision'] for r in report['matches']],['unknown','unknown'])
        self.assertEqual(len(net['identities']),2)
        self.assertEqual(len(net['alignment_ambiguities'][0]['candidate_matches']),2)
        for field in ['entities','states','relations','transitions','lineage','aliases']:
            self.assertEqual(net[field],before[field])

    def test_independent_same_matches_in_one_page_are_not_ambiguous(self):
        net,inp=self.alignment_inputs()
        ingest(net,page(entity(t=1)),self.frames,'S2','b2',[])
        ingest(net,page(entity(t=2)),self.frames,'S3','b3',[])
        inp.update(pairs=[['E1','E0'],['E3','E2']],states=list(net['states'].values()),
            left_entities=[dict(id='E1',state_ids=['T1']),dict(id='E3',state_ids=['T3'])],
            right_entities=[dict(id='E0',state_ids=['T0']),dict(id='E2',state_ids=['T2'])],
            compare_state_pairs=[dict(left=l,right=r,left_state=s,right_state=t)
                                 for l,r,s,t in [('E1','E0','T1','T0'),('E3','E2','T3','T2')]])
        matches=[self.match(),dict(self.match(),left='E3',right='E2',left_state='T3',right_state='T2',
                                  source_frame_ids=['F1','F2'])]
        report,_=align_page(net,inp,ScriptedMeter(dict(matches=matches)))
        self.assertEqual(len(net['entities']),2)
        self.assertEqual([r['decision'] for r in report['matches']],['same','same'])
        self.assertNotIn('alignment_ambiguities',net)

    def test_decoder_selects_entity_identity_before_shared_nested_observations(self):
        from lmformatenforcer import TokenEnforcerTokenizerData
        from script_core.backend import constrained_prefix
        import string
        import torch
        schema=decoder_schema(output_schema('temporal_observe',self.inputs))
        chars=string.printable;ids={c:i for i,c in enumerate(chars)}
        data=TokenEnforcerTokenizerData([(i,c,False) for c,i in ids.items()],
            lambda tokens: ''.join(chars[i] for i in tokens),len(chars),False,len(chars)+1)
        prefix_fn=constrained_prefix(data,schema,'temporal_observe')
        # Previously arbitrary field order allowed shared attributes/observations
        # to multiply every unresolved anyOf branch before selecting the ID.
        prefix='{"entities":[{"'
        sequence=[len(chars)+1]
        for char in prefix:
            self.assertIn(ids[char],prefix_fn(0,torch.tensor(sequence)))
            sequence.append(ids[char])
        allowed=prefix_fn(0,torch.tensor(sequence))
        self.assertIn(ids['e'],allowed)
        self.assertNotIn(ids['a'],allowed)

    def test_seven_frame_boundary_batch_fits_one_known_entity_row(self):
        frames=[dict(frame_id=f'F{i}',pts=float(i)) for i in range(7)]
        inp=dict(self.inputs,frames=frames)
        row=entity('E0',continuity='same')
        row['observations']=[entity('E0',t=i,continuity='same')['observations'][0] for i in range(7)]
        validate_output(page(row),output_schema('temporal_observe',inp))
