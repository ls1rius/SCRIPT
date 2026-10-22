"""Key source preservation, uncertainty, lazy lineage and bounded scale checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from script_core.budget import Meter, BudgetLedger
from script_core.config import DEFAULTS, digest, validate_config
from script_core.controller import answer_query
from script_core.key_memory import (assemble_keys, augment_directory, validate_keys, opened_keys,
    bounded_pointer_targets, build_hierarchy, validate_hierarchy, rank_hierarchy, key_candidates, rank_keys)
from script_core.scene_memory import build_directory, validate_directory, validate_matches, add_pointers
from script_core.schemas import output_schema, validate_output
import test_scene_memory as scene_fixtures
from test_scene_memory import SceneBackend
from run_scene_lazy import profile_config, get_directory, gate
from run_summary_graph import write


class KeyBackend(SceneBackend):
    def call(self, operation, inputs, timeout):
        if operation == 'scene_directory':
            self.seen.append((operation, deepcopy(inputs)))
            captions = inputs['captions']
            phases = [{'end_caption_id': captions[min(i+30, len(captions))-1]['id'],
                'description': 'C uses a cup.', 'entity_terms': ['cup']} for i in range(0, len(captions), 30)]
            return {'data': {'scenes': phases[:-1], 'final_scene': phases[-1]}}
        if operation == 'scene_key_information':
            self.seen.append((operation, deepcopy(inputs)))
            captions = inputs['captions']
            return {'data': {'keys': [{'kind': 'identity_attribute', 'description': 'A cup is used.',
                'entity_terms': ['cup'], 'before_caption_ids': [], 'after_caption_ids': [captions[-1]['id']]}]}}
        if operation == 'scene_review_keyed':
            result = super().call('scene_review', inputs, timeout)
            self.seen[-1] = (operation, deepcopy(inputs))
            return result
        if operation == 'scene_correspondence_lineage':
            self.seen.append((operation, deepcopy(inputs)))
            l, r = inputs['left']['entities'][0], inputs['right']['entities'][0]
            return {'data': {'matches': [{'left': {'entity': l['id'], 'caption_ids': [l['caption_ids'][0]]},
                'right': {'entity': r['id'], 'caption_ids': [r['caption_ids'][0]]},
                'relation': 'part_of', 'reason': 'Source-based candidate component relation'}]}}
        return super().call(operation, inputs, timeout)


class KeyMemoryTests(unittest.TestCase):
    def setup_inputs(self, folder):
        return scene_fixtures.SceneMemoryTests().setup_inputs(folder)

    def attach_keys(self, folder, directory, memory, cfg):
        backend = KeyBackend(memory)
        meter = Meter(backend, BudgetLedger(600., answer_reserve=0.), cfg, Path(folder)/'key_calls')
        try: augment_directory(directory, cfg, meter)
        finally: meter.close()
        return backend

    def test_key_sources_and_unknown_state_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, cfg, captions, d, _ = self.setup_inputs(folder)
            backend = self.attach_keys(folder, d, m, cfg)
            validate_directory(d, m.manifest)
            self.assertEqual(d['captions'], captions)
            for card in d['key_memory']['records']:
                self.assertFalse(card['pre_state_known'])
                self.assertIsNone(card['state_valid_until'])
                self.assertFalse(card['physical_identity_confirmed'])
                self.assertEqual(card['source_captions'][0], captions[int(card['after_caption_ids'][0][2:])-1])
            self.assertFalse(any(op in ('summary_graph','scene_correspondence','decode') for op,_ in backend.seen))
            self.assertTrue(all('question' not in inputs and 'options' not in inputs
                for op,inputs in backend.seen if op == 'scene_key_information'))

    def test_before_after_order_and_entity_grounding(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, _, _, d, _ = self.setup_inputs(folder)
            row = {'kind': 'state_change', 'description': 'A change candidate', 'entity_terms': ['cup','imaginary tool'],
                'before_caption_ids': ['TS1'], 'after_caption_ids': ['TS2']}
            cards,diag = assemble_keys(d,[{'keys':[row]}])
            self.assertEqual(cards[0]['entity_terms'],['cup'])
            self.assertTrue(cards[0]['pre_state_known'])
            row.update(before_caption_ids=['TS2'],after_caption_ids=['TS1'])
            cards,diag = assemble_keys(d,[{'keys':[row]}])
            self.assertEqual(cards,[])
            self.assertEqual(diag['rejected_keys']['invalid_before_after_order'],1)

    def test_static_distinctive_attributes_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, _, _, d, _ = self.setup_inputs(folder)
            row = {'kind':'identity_attribute','description':'Cup appearance','entity_terms':['cup'],
                'before_caption_ids':[],'after_caption_ids':['TS9']}
            cards,_ = assemble_keys(d,[{'keys':[row]}])
            self.assertEqual(cards[0]['kind'],'identity_attribute')
            self.assertEqual(cards[0]['last_observed_at'],9)

    def test_unknown_state_persistence_cannot_be_upgraded(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, cfg, _, d, _ = self.setup_inputs(folder)
            self.attach_keys(folder,d,m,cfg)
            d['key_memory']['records'][0]['state_valid_until']=180
            d['key_memory']['records_hash']=digest(d['key_memory']['records'])
            with self.assertRaises(ValueError):validate_keys(d)

    def test_modified_original_quote_is_rejected_even_with_rehashed_record(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, cfg, _, d, _ = self.setup_inputs(folder)
            self.attach_keys(folder,d,m,cfg)
            d['key_memory']['records'][0]['source_captions'][0]['text']='Fabricated state change'
            d['key_memory']['records_hash']=digest(d['key_memory']['records'])
            with self.assertRaises(ValueError):validate_keys(d)

    def test_cross_boundary_key_is_unread_until_both_scenes_open(self):
        with tempfile.TemporaryDirectory() as folder:
            _, _, _, _, d, _ = self.setup_inputs(folder)
            row={'kind':'split_merge','description':'Material transition','entity_terms':['cup'],
                'before_caption_ids':['TS60'],'after_caption_ids':['TS61']}
            cards,diag=assemble_keys(d,[{'keys':[row]}])
            d['key_memory']={'records':cards,'positions':{cards[0]['id']:0}}
            for s in d['scenes']:s['key_ids']=[cards[0]['id']] if s['id'] in cards[0]['scene_ids'] else []
            first,second=d['scenes'][:2]
            self.assertEqual(opened_keys(d,{first['id']}),[])
            self.assertEqual(opened_keys(d,{first['id'],second['id']}),cards)

    def test_progressive_partition_calls_are_bounded_and_keep_all_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, cfg, captions, _, _ = self.setup_inputs(folder)
            cfg=profile_config(cfg,'scene_hierarchical')
            backend=KeyBackend(m)
            meter=Meter(backend,BudgetLedger(600.,answer_reserve=0.),cfg,Path(folder)/'calls')
            try:d=build_directory(m.manifest,cfg,meter)
            finally:meter.close()
            validate_directory(d,m.manifest)
            batches=[i['captions'] for op,i in backend.seen if op=='scene_directory']
            self.assertEqual([len(b) for b in batches],[120,60])
            self.assertEqual(d['captions'],captions)
            self.assertTrue(all(len(i['captions'])<=32 for op,i in backend.seen if op=='scene_key_information'))
            self.assertFalse(any(op=='summary_graph' for op,_ in backend.seen))
            self.assertTrue(all(len(i['texts'])<=16 for op,i in backend.seen if op=='embed'))

    def large_directory(self,n=2048):
        scenes=[{'id':f'S{i:05d}','interval':[i*30,(i+1)*30], 'description':'Repeated processing',
            'entity_terms':['material'],'caption_ids':[]} for i in range(n)]
        return {'video_id':'long','scenes':scenes,'vectors':[[1.,0.]]*n,'pointers':[],
            'captions':[],'build_diagnostics':{}}

    def test_long_pointer_construction_has_bounded_comparisons(self):
        d=self.large_directory()
        cfg=profile_config(deepcopy(DEFAULTS),'scene_hierarchical')
        add_pointers(d,d['vectors'],2,cfg)
        n=len(d['scenes'])
        self.assertLessEqual(d['build_diagnostics']['semantic_pointer_comparisons'],n*64)
        self.assertLessEqual(len(d['pointers']),3*n)
        self.assertEqual(sum(p['kind']=='temporal_next' for p in d['pointers']),n-1)

    def test_hierarchy_retrieval_fanout_is_bounded_at_long_scale(self):
        d=self.large_directory()
        cfg=profile_config(deepcopy(DEFAULTS),'scene_hierarchical')
        with tempfile.TemporaryDirectory() as folder:
            backend=KeyBackend(None)
            meter=Meter(backend,BudgetLedger(600.,answer_reserve=0.),cfg,Path(folder)/'calls')
            try:
                build_hierarchy(d,cfg,meter)
                validate_hierarchy(d)
                rankings,trace=rank_hierarchy(d,'material',[1.,0.],[0.,2048*30.],cfg,meter)
            finally:meter.close()
        self.assertLessEqual(len(rankings),2)
        self.assertLessEqual(max(t['candidate_nodes'] for t in trace),16)
        self.assertEqual(d['hierarchy']['depth'],4)

    def test_hierarchy_does_not_compress_or_mutate_key_records(self):
        with tempfile.TemporaryDirectory() as folder:
            m, _, cfg, _, d, _=self.setup_inputs(folder)
            self.attach_keys(folder,d,m,cfg)
            original=deepcopy(d['key_memory'])
            backend=KeyBackend(m)
            meter=Meter(backend,BudgetLedger(600.,answer_reserve=0.),cfg,Path(folder)/'tree')
            try:build_hierarchy(d,cfg,meter)
            finally:meter.close()
            self.assertEqual(d['key_memory'],original)
            validate_keys(d)

    def test_common_term_postings_are_capped_and_rare_early_key_survives(self):
        cards=[{'id':f'K{i}'} for i in range(10000)]
        d={'key_memory':{'records':cards,'postings':{'material':[c['id'] for c in cards],'serialqx':['K1']}}}
        ids,touched=key_candidates(d,'material serialqx',64)
        self.assertIn('K1',ids)
        self.assertLessEqual(len(ids),64)
        self.assertLessEqual(touched,64*8)

    def test_bad_hierarchy_cycle_is_rejected(self):
        d=self.large_directory(2)
        d['hierarchy']={'root':'P','fanout':8,'nodes':{'P':{'id':'P','leaf':False,'children':['P']}}}
        with self.assertRaises(ValueError):validate_hierarchy(d)

    def run_case(self,arm='scene_keys',expand=False,scope='local'):
        with tempfile.TemporaryDirectory() as folder:
            m,meta,cfg,_,_,q=self.setup_inputs(folder)
            cfg=profile_config(cfg,arm)
            backend=KeyBackend(m,expand)
            backend.scope=scope
            meter=Meter(backend,BudgetLedger(600.,answer_reserve=0.),cfg,Path(folder)/'directory')
            try:d=build_directory(m.manifest,cfg,meter)
            finally:meter.close()
            backend.seen=[]
            result=answer_query(m,q,meta,cfg,backend,Path(folder)/'query',scene_directory=d)
            self.assertIsNone(result['error'],result['error'])
            return result,backend,d

    def test_global_coverage_applies_to_keyed_and_hierarchical_reviews(self):
        for arm in ('scene_keys', 'scene_hierarchical'):
            with self.subTest(arm=arm):
                r, backend, directory = self.run_case(arm, scope='global')
                d = r['scene_memory']
                self.assertGreater(len(d['opened_scenes']), 1)
                self.assertTrue(d['text_sufficient'])
                self.assertTrue(d['reviews'][-1]['coverage_check']['met'])
                self.assertEqual(len(d['local_graphs']), len(d['opened_scenes']))

    def test_keyed_query_opens_one_scene_and_never_leaks_unopened_quotes(self):
        r,b,d=self.run_case()
        opened=set(r['scene_memory']['opened_scenes'])
        allowed={c for s in d['scenes'] if s['id'] in opened for c in s['caption_ids']}
        self.assertEqual(len(opened),1)
        review=next(i for op,i in b.seen if op=='scene_review_keyed')
        for card in review['key_memory']:
            self.assertTrue(set(card['before_caption_ids']+card['after_caption_ids'])<=allowed)
        self.assertEqual(set(r['scene_memory']['local_graphs']),opened)
        self.assertFalse(any(op=='scene_key_information' for op,_ in b.seen))

    def test_typed_lineage_does_not_become_same_instance(self):
        r,b,_=self.run_case(expand=True)
        match=r['scene_memory']['correspondences'][0]['matches'][0]
        self.assertEqual(match['relation'],'part_of')
        self.assertEqual(match['decision'],'unknown')
        self.assertEqual(match['status'],'text_inferred')
        self.assertFalse(match['physical_identity_confirmed'])
        self.assertTrue(any(op=='scene_correspondence_lineage' for op,_ in b.seen))

    def test_hierarchical_query_uses_bounded_navigation_and_lazy_graphs(self):
        r,b,_=self.run_case('scene_hierarchical')
        self.assertTrue(r['scene_memory']['hierarchy_enabled'])
        self.assertTrue(r['scene_memory']['hierarchy_retrieval'])
        self.assertEqual(len(r['scene_memory']['opened_scenes']),1)
        self.assertTrue(all(len(i['documents'])<=64 for op,i in b.seen if op=='retrieve'))

    def test_profiles_share_models_budget_and_keep_old_modes_disabled(self):
        profiles=[profile_config(deepcopy(DEFAULTS),a) for a in ('c0','scene_lazy','scene_keys','scene_hierarchical')]
        self.assertTrue(all(c['models']==profiles[0]['models'] and c['budget']==profiles[0]['budget'] for c in profiles))
        self.assertFalse(profiles[1]['scene_memory']['key_information'])
        self.assertFalse(profiles[1]['scene_memory']['hierarchical_directory'])
        self.assertTrue(profiles[2]['scene_memory']['key_information'])
        self.assertFalse(profiles[2]['scene_memory']['hierarchical_directory'])
        for c in profiles:validate_config(c)

    def test_runner_builds_and_reuses_profile_specific_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            m,_,cfg,_,_,q=self.setup_inputs(folder)
            cfg=profile_config(cfg,'scene_keys')
            packet={'question':q,'manifest':m.manifest}
            backend=KeyBackend(m)
            directory=get_directory(Path(folder),packet,backend,cfg)
            count=len(backend.seen)
            again=get_directory(Path(folder),packet,backend,cfg)
            self.assertEqual(directory,again)
            self.assertEqual(count,len(backend.seen))
            self.assertTrue((Path(folder)/'directories/keys/v.json').exists())
            self.assertIn('cost_summary',directory)

    def test_smoke_gate_rejects_partition_change_in_key_only_arm(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write(root/'experiment.json',{'arms':{'c0':'control','scene_lazy':'basic','scene_keys':'keys'}})
            write(root/'smoke/ids.json',['q'])
            write(root/'evaluation_only.json',{'q':{'videotree':None}})
            write(root/'inputs/q.json',{'question':{'video_id':'v'}})
            for arm in ('c0','scene_lazy','scene_keys'):
                write(root/'smoke/arms'/arm/'runs'/digest('q')[:20]/'result.json',{
                    'error':None,'cost_summary':{'budget_violation':False},
                    'scene_memory':{'opened_scenes':['s1'],'local_graphs':{'s1':{}},'pixel_reads':0}})
            write(root/'smoke/directories/basic/v.json',{'scenes':[{'id':'s1','caption_ids':['TS1']}]})
            write(root/'smoke/directories/keys/v.json',{'scenes':[{'id':'s1','caption_ids':['TS2'],'key_ids':[]}]})
            self.assertFalse(gate(root))
            status=json.loads((root/'smoke/status.json').read_text())
            self.assertTrue(any('partition' in str(f) for f in status['failures']))


if __name__=='__main__':unittest.main()
