"""Check original algorithm reuse, matched evidence, and strict budget accounting."""
from copy import deepcopy
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import torch

from script_core.config import load_config,digest
from script_core.videotree_aligned import contiguous_kmeans,width,directory,visit_order,defaults
from script_core.aligned_pipeline import pair,fit_context,paired_inputs,baseline
from script_core.budget import ResourceExhausted


def packet(folder,n=16):
    f=Path(folder)/'v.pt';torch.save(torch.tensor([[1.,0.]]*(n//2)+[[0.,1.]]*(n-n//2)),f)
    return dict(question=dict(question_id='v',video_id='v',question='Which change?',options=['a','b','c','d','e']),
        captions=[dict(id=f'TS{i}',interval=[i,i+1],text=f'original second {i}') for i in range(n)],
        frames=[dict(frame_id=f'F{i}',pts=float(i),path='/unused',width=20,height=20) for i in range(n)],feature_uri=str(f))

class ScoreModel:
    max_new_tokens=1024
    def __init__(self,scores=3):self.score=scores;self.calls=[]
    def set_post_process_fn(self,fn):self.fn=fn
    def forward(self,head,prompts):
        import re
        count=int(re.search(r'exactly (\d+) listed frames',prompts[-1]).group(1))
        self.calls.append((head,deepcopy(prompts),count))
        return [self.score]*count,dict(response='frame relevance: '+str([self.score]*count))

class Tokenizer:
    def apply_chat_template(self,messages,**kwargs):
        return list(range(sum(len(m['content']) for m in messages)//4))

class Backend:
    def __init__(self,stop=False):self.seen=[];self.stop=stop
    def call(self,op,inp,timeout):
        self.seen.append((op,deepcopy(inp)))
        if op=='prepare_vlm':return dict(ready=True)
        if op in ('videotree_width','videotree_contiguous_width'):
            return dict(data=dict(width=dict(cluster_ids_x=[0]*8+[1]*8),relevance=dict(cluster_relevance=[2,3]),alignment={}))
        if op=='videotree_depth':return dict(data=dict(selected_seconds=[0,7,8,15]))
        if op=='temporal_summary':return dict(data=dict(description='whole source scene'))
        if op=='temporal_observe':
            assert 'question' not in inp and 'options' not in inp
            if self.stop:raise ResourceExhausted('fixture budget exhausted')
            fid=inp['frames'][0]['frame_id']
            known=inp['entities']
            return dict(data=dict(entities=[dict(entity_ref=known[0]['id'] if known else 'new',label='box',
                attributes=[dict(field='color',value='red',source_frame_ids=[fid])],
                observations=[dict(frame_id=fid,state='closed',continuity='same' if known else 'initial',
                    cause=dict(action=None,actor=None,tool=None,basis='unknown',source_frame_ids=[]))])],
                relations=[],more_entities=False,unresolved=''))
        if op=='temporal_align':
            p=inp['compare_state_pairs'][0]
            sources=inp['identity_evidence_pairs'][0]
            return dict(data=dict(matches=[dict(**p,decision='same',state_continuity='same',reason='same distinguishing appearance',
                source_frame_ids=[f['frame_id'] for f in inp['frames']],ended_relation_ids=[],
                left_source_frame_id=sources['left_source_frame_ids'][0],right_source_frame_id=sources['right_source_frame_ids'][0])]))
        if op=='videotree_qa':return dict(data=dict(selected_option='0',text='prediction: A'))
        raise AssertionError(op)

class AlignedTests(unittest.TestCase):
    def test_fixed_k_exact_contiguous_optimum(self):
        x=torch.tensor([[1.,0.],[1.,.1],[0.,1.],[.1,1.],[1.,0.]])
        labels,centers=contiguous_kmeans(x,3)
        self.assertEqual(len(set(labels.tolist())),3)
        self.assertEqual(labels.tolist(),sorted(labels.tolist()))
        z=torch.nn.functional.normalize(x,dim=1)
        def cost(bounds):
            return sum(float(((z[a:b]-z[a:b].mean(0))**2).sum()) for a,b in zip(bounds,bounds[1:]))
        actual=[0]+[i for i in range(1,len(x)) if labels[i]!=labels[i-1]]+[len(x)]
        best=min(cost([0,*inner,len(x)]) for inner in itertools.combinations(range(1,len(x)),2))
        self.assertAlmostEqual(cost(actual),best,places=6)
        for cid in range(3):self.assertTrue(torch.allclose(centers[cid],x[labels==cid].mean(0)))

    def test_same_looking_nonadjacent_scenes_stay_distinct(self):
        x=torch.tensor([[1.,0.]]*3+[[0.,1.]]*3+[[1.,0.]]*3)
        labels,_=contiguous_kmeans(x,3)
        self.assertEqual(labels.tolist(),[0]*3+[1]*3+[2]*3)

    def test_invalid_features_and_counts(self):
        for x,k in [(torch.zeros(3,2),2),(torch.ones(3,2),4),(torch.tensor([[float('nan'),1.]]),1)]:
            with self.assertRaises(ValueError):contiguous_kmeans(x,k)

    def test_original_width_prompt_and_growth_are_reused(self):
        import adaptive_breath_expansion as original
        saved=original.kmeans
        with tempfile.TemporaryDirectory() as tmp:
            p=packet(tmp,40);model=ScoreModel(1)
            result=width(dict(query=dict(id='v',question=p['question']['question'],options=p['question']['options']),
                query_id='v',captions=[c['text'] for c in p['captions']],duration=40,feature_uri=p['feature_uri']),model,'cpu')
            self.assertEqual([c[2] for c in model.calls],[8,16,32])
            from prompts import PromptFactory
            self.assertEqual(model.calls[0][0],PromptFactory().get('cap_score').head)
            self.assertEqual(len(result['relevance']['cluster_relevance']),32)
        self.assertIs(original.kmeans,saved)

    def test_original_width_stops_at_four_high_relevance_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=packet(tmp,40);model=ScoreModel(3)
            result=width(dict(query=dict(id='v',question='Which?',options=['a']*5),query_id='v',
                captions=[c['text'] for c in p['captions']],duration=40,feature_uri=p['feature_uri']),model,'cpu')
            self.assertEqual([c[2] for c in model.calls],[8]);self.assertEqual(len(result['width']['tree_node']),8)

    def test_original_kmeans_is_restored_on_failure(self):
        import adaptive_breath_expansion as original
        saved=original.kmeans
        class Broken(ScoreModel):
            def forward(self,*args):raise RuntimeError('fixture error')
        with tempfile.TemporaryDirectory() as tmp:
            p=packet(tmp,16)
            with self.assertRaises(RuntimeError):width(dict(query=dict(id='v',question='q',options=['a']*5),
                captions=['caption']*16,duration=16,feature_uri=p['feature_uri']),Broken(),'cpu')
        self.assertIs(original.kmeans,saved)

    def test_directory_representative_matches_original_and_no_duration_split(self):
        from adaptive_breath_expansion import find_closest_points_per_cluster
        p=dict(question=dict(video_id='v'),captions=[dict(id=str(i),interval=[i,i+1],text='c') for i in range(100)])
        x=torch.tensor([[1.+i/100,1.] for i in range(100)])
        w=dict(width=dict(cluster_ids_x=[0]*100),relevance=dict(cluster_relevance=[3]),alignment={})
        d=directory(p,x,w)
        self.assertEqual(d['scenes'][0]['interval'],[0,100])
        center=x.mean(0).unsqueeze(0)
        expected=find_closest_points_per_cluster(x,torch.zeros(100,dtype=torch.long),center)[0][0]
        self.assertEqual(d['scenes'][0]['representative_order'][0],expected)

    def test_pointers_allow_reverse_and_visit_every_selected_scene(self):
        d=dict(scenes=[dict(id=f'S{i}',chronological_order=[i],interval=[i,i+1],relevance=3 if i==4 else 1) for i in range(5)],
            pointers=[dict(source=f'S{i}',target=f'S{i+1}') for i in range(4)])
        order=visit_order(d,list(range(5)))
        self.assertEqual(order,['S4','S3','S2','S1','S0'])

    def test_paired_run_shares_frames_facts_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=packet(tmp);cfg=load_config();b=Backend()
            with patch('script_core.aligned_pipeline.fit_context',wraps=lambda built,packet,cfg,final:fit_context(built,packet,cfg,final,Tokenizer())):
                results=pair(p,b,cfg,Path(tmp)/'run')
            a,c=results.values()
            self.assertIsNone(a['error'],a['error']);self.assertIsNone(c['error'],c['error'])
            for field in ('selected_seconds','selected_frame_ids','observed_frame_ids','context_hash','shared_construction_hash'):
                self.assertEqual(a[field],c[field])
            self.assertEqual(set(a['observed_frame_ids']),{'F0','F7','F8','F15'})
            self.assertEqual(a['cost_summary']['total_cap'],600)
            self.assertEqual(a['shared_cost_summary'],c['shared_cost_summary'])
            self.assertEqual(a['identity_comparisons'],1)
            self.assertEqual(len([x for x in b.seen if x[0]=='videotree_contiguous_width']),1)
            self.assertEqual(len([x for x in b.seen if x[0]=='videotree_qa']),2)
            ops={op for op,_ in b.seen}
            self.assertFalse(ops & {'clip_text','scene_scope','temporal_review'})
            observed={f['frame_id'] for op,inp in b.seen if op in ('temporal_observe','temporal_align') for f in inp['frames']}
            self.assertTrue(observed<={'F0','F7','F8','F15'})

    def test_budget_interruption_is_paired_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=packet(tmp);cfg=load_config()
            from script_core.budget import Meter
            original=Meter.json
            def stop_json(meter,operation,*args,**kwargs):
                if operation=='temporal_observe':raise ResourceExhausted('fixture online allowance exhausted')
                return original(meter,operation,*args,**kwargs)
            with patch.object(Meter,'json',stop_json),patch('script_core.aligned_pipeline.fit_context',wraps=lambda built,packet,cfg,final:fit_context(built,packet,cfg,final,Tokenizer())):
                results=pair(p,Backend(),cfg,Path(tmp)/'run')
            a,b=results.values()
            self.assertIsNone(a['error']);self.assertEqual(a['incomplete'],b['incomplete']);self.assertTrue(a['incomplete'])
            self.assertEqual(a['context_hash'],b['context_hash'])

    def test_original_baseline_uses_no_visual_memory_or_changed_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=packet(tmp);backend=Backend();r=baseline(p,backend,load_config(),Path(tmp)/'base')
            self.assertIsNone(r['error'],r['error'])
            self.assertEqual([op for op,_ in backend.seen],['prepare_vlm','videotree_width','videotree_depth','videotree_qa'])
            from script_core.compatibility import qa_inputs
            self.assertEqual(r['qa_inputs'],qa_inputs(dict(p['question'],id='v'),p['captions'],[0,7,8,15]))
            self.assertEqual(r['cost_summary']['total_cap'],600)

    def test_context_fits_both_renderings_with_same_retained_facts(self):
        from script_core.temporal_entity import new_network,ingest
        n=new_network()
        for i in range(10):
            row=dict(entity_ref='new',label='box',attributes=[dict(field='appearance',value='red '*50,source_frame_ids=['F0'])],
                observations=[dict(frame_id='F0',state='closed',continuity='initial',cause=dict(action=None,actor=None,tool=None,basis='unknown',source_frame_ids=[]))])
            ingest(n,dict(entities=[row],relations=[],more_entities=False,unresolved=''),[dict(frame_id='F0',pts=0)],'S0','b',[])
        with tempfile.TemporaryDirectory() as tmp:
            p=packet(tmp);cfg=load_config();cfg['models']['max_input_tokens']=1800
            built=dict(network=n,query=dict(p['question'],id='v'),directory=dict(scenes=[dict(id='S0',interval=[0,16],relevance=3)]),selected_seconds=[0])
            fitted=fit_context(built,p,cfg,tokenizer=Tokenizer())
            self.assertLess(fitted['selection']['selected_entities'],10)
            self.assertLessEqual(max(fitted['token_counts']),1800)
            self.assertEqual(fitted['context_hash'],digest(fitted['context']))

if __name__=='__main__':unittest.main()
