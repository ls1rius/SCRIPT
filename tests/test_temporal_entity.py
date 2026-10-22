import copy
import tempfile
import unittest
from pathlib import Path
from script_core.temporal_entity import (SETTINGS,partition,new_network,ingest,align,entity_context,output_schema)
from script_core.temporal_pipeline import run
from script_core.config import load_config
from script_core.schemas import validate_output,decoder_schema


def frames(start,end):
    return [dict(frame_id=f'F{i}',pts=float(i),path='/unused',width=20,height=20) for i in range(start,end)]

def row(ref='new',t=0,continuity='initial',state='closed',value='red'):
    return dict(entity_ref=ref,label='box',attributes=[dict(field='color',value=value,source_frame_ids=[f'F{t}'])],
        observations=[dict(frame_id=f'F{t}',state=state,continuity=continuity,
            cause=dict(action=None,actor=None,tool=None,basis='unknown',source_frame_ids=[]))])

def report(*rows,relations=None):
    return dict(entities=list(rows),relations=relations or [],more_entities=False,unresolved='')

class TemporalEntityTests(unittest.TestCase):
    def test_visual_aba_partition_and_center_order(self):
        caps=[dict(id=f'TS{i}',interval=[i,i+1],text='source') for i in range(12)]
        d=partition('v',[[1,0]]*4+[[0,1]]*4+[[1,0]]*4,caps,dict(SETTINGS,penalty=.2))
        self.assertEqual([s['interval'] for s in d['scenes']],[[0,4],[4,8],[8,12]])
        self.assertEqual(d['scenes'][0]['center'],d['scenes'][2]['center'])
        self.assertTrue(any(p['source']=='S0' and p['target']=='S2' and p['bidirectional'] for p in d['pointers']))
        self.assertEqual(d['scenes'][0]['representative_order'],[0,1,2,3])
        self.assertFalse(any('entities' in s for s in d['scenes']))

    def test_new_attributes_do_not_create_states(self):
        n=new_network();ingest(n,report(row()),frames(0,4),'S0','b0',[])
        ingest(n,report(row('E0',2,'same','same wording differs','dark red')),frames(0,4),'S0','b1',['E0'])
        self.assertEqual(len(n['states']),1);self.assertEqual(len(n['entities']['E0']['attributes']),2)
        self.assertEqual(n['states']['T0']['observed_support_interval'],[0,2])
        self.assertIsNone(n['states']['T0']['valid_to'])

    def test_change_bounds_and_unknown_cause(self):
        n=new_network();ingest(n,report(row()),frames(0,6),'S0','b',[])
        ingest(n,report(row('E0',4,'changed','open')),frames(0,6),'S0','b1',['E0'])
        self.assertEqual(len(n['states']),2)
        self.assertEqual(n['transitions'][0]['change_time_bounds'],[0,4])
        self.assertEqual(n['transitions'][0]['cause']['basis'],'unknown')
        self.assertIsNone(n['transitions'][0]['cause']['action'])
        self.assertIsNone(n['states']['T0']['valid_to'])

    def test_uncertainty_and_foreign_evidence(self):
        n=new_network();ingest(n,report(row()),frames(0,6),'S0','b',[])
        ingest(n,report(row('E0',4,'uncertain','possibly open')),frames(0,6),'S0','b1',['E0'])
        self.assertEqual(len(n['states']),1);self.assertEqual(len(n['observations']),1)
        bad=row(t=2);bad['attributes'][0]['source_frame_ids']=['foreign']
        with self.assertRaises(ValueError):ingest(new_network(),report(bad),frames(0,6),'S0','b',[])

    def test_relations_keep_history_and_state_links(self):
        n=new_network()
        rel=dict(subject='B0',object='B1',predicate='on',frame_id='F0',update='present',source_frame_ids=['F0'])
        ingest(n,report(row(),row(value='blue'),relations=[rel]),frames(0,6),'S0','b',[])
        self.assertEqual(n['states']['T0']['relation_ids'],['R0'])
        ended=dict(rel,subject='E0',object='E1',frame_id='F4',update='end',source_frame_ids=['F4'])
        ingest(n,report(row('E0',4,'changed','on floor'),relations=[ended]),frames(0,6),'S0','b1',['E0','E1'])
        self.assertEqual(n['relations'][0]['valid_to_bounds'],[0,4])
        self.assertEqual(len(n['relations']),1)

    def test_backward_cross_scene_transfer_gap_and_unique_ids(self):
        n=new_network();ingest(n,report(row(t=10)),frames(10,12),'S1','a',[])
        ingest(n,report(row(t=0)),frames(0,2),'S0','b',[])
        match=dict(left='E1',right='E0',left_state='T1',right_state='T0',decision='same',
                   state_continuity='same',reason='same distinct box',source_frame_ids=['F0','F10'])
        align(n,dict(matches=[match]),frames(0,12),[['E1','E0']])
        self.assertEqual(n['entities']['E1']['state_ids'],['T1','T0'])
        self.assertEqual(n['states']['T0']['same_observed_state_as'],'T1')
        self.assertEqual(n['transitions'],[])
        self.assertIsNone(n['states']['T1']['valid_to'])
        ingest(n,report(row(t=20)),frames(20,22),'S2','c',[])
        self.assertEqual(set(n['entities']),{'E1','E2'})

    def test_lineage_is_not_identity(self):
        n=new_network();ingest(n,report(row(t=0)),frames(0,2),'S0','a',[])
        ingest(n,report(row(t=10)),frames(10,12),'S1','b',[])
        match=dict(left='E1',right='E0',left_state='T1',right_state='T0',decision='derived_from',
            state_continuity='unknown',reason='cut piece',source_frame_ids=['F0','F10'])
        align(n,dict(matches=[match]),frames(0,12),[['E1','E0']])
        self.assertEqual(len(n['entities']),2);self.assertEqual(len(n['lineage']),1)

    def test_covisible_entities_cannot_merge(self):
        n=new_network();ingest(n,report(row(),row(value='blue')),frames(0,2),'S0','a',[])
        m=dict(left='E0',right='E1',left_state='T0',right_state='T1',decision='same',
               state_continuity='unknown',reason='same label',source_frame_ids=['F0'])
        with self.assertRaises(ValueError):align(n,dict(matches=[m]),frames(0,2),[['E0','E1']])

    def test_schema_rejects_fake_sources_and_lmfe_accepts_schema(self):
        from lmformatenforcer import JsonSchemaParser
        inp=dict(frames=frames(0,6),entities=[],page_size=4)
        schema=output_schema('temporal_observe',inp)
        JsonSchemaParser(decoder_schema(schema))
        validate_output(report(row()),schema)
        bad=report(row());bad['entities'][0]['attributes'][0]['source_frame_ids']=['F999']
        with self.assertRaises(ValueError):validate_output(bad,schema)

    def test_pipeline_three_arms_same_entry_query_independent_graph(self):
        caps=[dict(id=f'TS{i}',interval=[i,i+1],text='box opens') for i in range(12)]
        d=partition('v',[[1,0]]*6+[[0,1]]*6,caps,dict(SETTINGS,penalty=.2,min_samples=2))
        packet=dict(question=dict(question_id='q',video_id='v',question='What changes?',options=['a','b','c','d','e']))
        class Backend:
            def call(self,op,inp,timeout):
                if op=='clip_text':return dict(vectors=[[1,0]]*6,chunk_counts=[1]*6,content_token_counts=[1]*6)
                if op=='scene_scope':return dict(data=dict(scope='multi_scene',reason='two states'))
                if op=='temporal_observe':
                    assert 'question' not in inp and 'options' not in inp
                    known=inp['entities']; t=int(inp['frames'][-1]['pts'])
                    return dict(data=report(row(known[0]['id'] if known else 'new',t,'same' if known else 'initial')))
                if op=='temporal_align':
                    a,b=inp['pairs'][0];s=inp['compare_state_pair']
                    sources=inp['identity_evidence_pairs'][0]
                    return dict(data=dict(matches=[dict(left=a,right=b,**s,decision='same',state_continuity='unknown',reason='same red box',
                        source_frame_ids=[f['frame_id'] for f in inp['frames']],ended_relation_ids=[],
                        left_source_frame_id=sources['left_source_frame_ids'][0],right_source_frame_id=sources['right_source_frame_ids'][0])]))
                if op=='temporal_review':return dict(data=dict(sufficient=True,reason='known',gap='',next_scene='stop',entity_ids=[]))
                if op=='temporal_qa_check':return dict(input_tokens=100)
                if op=='videotree_qa':return dict(data=dict(selected_option='0',text='a'))
                raise AssertionError(op)
        with tempfile.TemporaryDirectory() as temp:
            cfg=load_config();cfg['query']['temporal_entity_source']='images'; results=[]
            for arm in ('cluster_qa','temporal_local','temporal_cross'):
                r=run(packet,d,frames(0,12),Backend(),cfg,Path(temp)/arm,arm)
                self.assertIsNone(r['error'],r['error']);results.append(r)
            self.assertEqual({r['retrieval']['entry_scene'] for r in results},{'S0'})
            self.assertEqual(results[0]['pixel_frame_count'],0)
            self.assertGreater(results[1]['entity_count'],0)
            self.assertEqual(results[2]['opened_scenes'],['S0','S1'])
            self.assertEqual(results[2]['identity_comparisons'],1)

if __name__=='__main__':unittest.main()

class ClipTextChunkTests(unittest.TestCase):
    def test_long_clip_query_preserves_all_content_tokens(self):
        import torch
        from types import SimpleNamespace
        from transformers import AutoTokenizer
        from script_core.backend import LocalModels
        cfg=load_config()
        tokenizer=AutoTokenizer.from_pretrained(cfg['models']['visual_encoder'],local_files_only=True)
        seen=[]
        class Encoder:
            device=torch.device('cpu')
            config=SimpleNamespace(text_config=SimpleNamespace(max_position_embeddings=77))
            def encode_text(self,ids,attention_mask):
                for row,mask in zip(ids,attention_mask):
                    seen.extend(row[1:int(mask.sum())-1].tolist())
                return torch.ones((len(ids),1280))
        backend=LocalModels(cfg);backend.loaded['visual_encoder']=(Encoder(),None)
        backend.loaded['clip_tokenizer']=tokenizer
        text='the person moves a red wooden chair across the room '*40
        expected=tokenizer.encode(text,add_special_tokens=False,truncation=False)
        value=backend.clip_text([text])
        self.assertEqual(seen,expected)
        self.assertEqual(value['content_token_counts'],[len(expected)])
        self.assertGreater(value['chunk_counts'][0],1)
        self.assertEqual(len(value['vectors'][0]),1280)
