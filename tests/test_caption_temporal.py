"""Summary-only extraction, source grounding, identities and end-to-end routing."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

from script_core import caption_temporal as text
from script_core.config import load_config
from script_core.schemas import output_schema,validate_output
from script_core.temporal_entity import new_network
from script_core.temporal_pipeline import run


def captions():
    return [dict(id=f'C{i}',interval=[i,i+1],text=s) for i,s in enumerate([
        'C holds a closed red box.', 'C holds the closed red box.',
        'C opens the red box.', 'C holds the open red box.'])]


def row(ref='new',cid='C0',state='closed',continuity='initial'):
    return dict(entity_ref=ref,label='box',attributes=[],observations=[dict(caption_id=cid,state=state,
        continuity=continuity,cause=dict(action=None,actor=None,tool=None,basis='unknown',source_caption_ids=[]))])


def report(*rows):
    return dict(entities=list(rows),relations=[],more_entities=False,unresolved='')


class CaptionTemporalTests(unittest.TestCase):
    def test_summary_is_the_default_entity_source(self):
        self.assertEqual(load_config()['query']['temporal_entity_source'],'summary')

    def test_schema_accepts_caption_ids_and_rejects_visual_evidence(self):
        inp=dict(captions=captions()[:2],entities=[],already_reported=[],page_size=12)
        schema=output_schema('caption_temporal_observe',inp)
        validate_output(report(row()),schema)
        self.assertNotIn('frame_id',json.dumps(schema))
        bad=report(row(cid='F0'))
        with self.assertRaises(ValueError):validate_output(bad,schema)

    def test_changed_state_keeps_unknown_cause_and_caption_provenance(self):
        net=new_network()
        text.commit_page(net,report(row()),captions()[:2],'S0','b0',[])
        text.commit_page(net,report(row('E0','C2','opens','changed')),captions()[2:],'S0','b1',['E0'])
        self.assertEqual(len(net['states']),2)
        self.assertEqual(net['states']['T0']['source_caption_ids'],['C0'])
        self.assertEqual(net['transitions'][0]['cause']['basis'],'unknown')
        self.assertEqual(net['source_captions']['C0'],captions()[0])
        self.assertNotIn('source_frame_ids',json.dumps(net))
        self.assertNotIn('visual_model_assertion',json.dumps(net))

    def test_unchanged_observation_extends_support_without_new_state(self):
        net=new_network();text.commit_page(net,report(row()),captions(),'S0','b0',[])
        r=row('E0');r['observations']=[dict(caption_id='C1',continuity='same')]
        text.commit_page(net,report(r),captions(),'S0','b1',['E0'])
        self.assertEqual(len(net['states']),1)
        self.assertEqual(net['states']['T0']['source_caption_ids'],['C0','C1'])
        self.assertEqual(net['transitions'],[])
        self.assertIsNone(net['states']['T0']['valid_to'])

    def test_foreign_sources_and_unstated_values_are_rejected_atomically(self):
        for field in ['citation','attribute','state','cause']:
            with self.subTest(field=field):
                net=new_network();before=deepcopy(net);r=row()
                if field=='citation':r['observations'][0]['caption_id']='C99'
                if field=='attribute':r['attributes']=[dict(field='texture',value='striped',source_caption_ids=['C0'])]
                if field=='state':r['observations'][0]['state']='blue metal'
                if field=='cause':r['observations'][0]['cause']=dict(action='paints',actor='C',tool=None,basis='inferred_action',source_caption_ids=['C0'])
                with self.assertRaises(ValueError):text.commit_page(net,report(r),captions(),'S0','b0',[])
                self.assertEqual(net,before)

    def test_source_text_is_not_rewritten_by_status_translation(self):
        source=dict(id='C0',interval=[0,1],text='visual_model_assertion')
        self.assertEqual(text.map_evidence(source),source)

    def test_identity_merge_keeps_separate_state_fragments_and_inferred_status(self):
        net=new_network()
        text.commit_page(net,report(row()),captions()[:2],'S0','b0',[])
        text.commit_page(net,report(row(cid='C2',state='opens')),captions()[2:],'S1','b1',[])
        class Meter:
            def json(self,op,inputs,required):
                self.inputs=inputs
                return dict(matches=[dict(left=dict(entity='E1',caption_ids=['C2']),
                    right=dict(entity='E0',caption_ids=['C0']),relation='same_instance',reason='same box in text')]),{}
        meter=Meter()
        with tempfile.TemporaryDirectory() as tmp:text.cross_align(net,['E1'],['E0'],captions(),meter,tmp)
        self.assertEqual(list(net['entities']),['E0'])
        self.assertEqual(len(net['states']),2)
        self.assertEqual(net['transitions'],[])
        self.assertFalse(net['identities'][0]['physical_identity_confirmed'])
        self.assertEqual(net['identities'][0]['status'],'text_inferred')
        self.assertNotIn('frames',meter.inputs)

    def test_all_three_paths_use_text_without_opening_image_files(self):
        caps=captions();packet=dict(captions=caps,question=dict(question_id='q',video_id='v',question='What happens overall?',options=['box','other','a','b','c']))
        d=dict(captions=caps,scenes=[dict(id=f'S{i}',interval=[2*i,2*i+2],center=[1.,0.],
            chronological_order=list(range(2*i,2*i+2)),caption_ids=[c['id'] for c in caps[2*i:2*i+2]],description='box') for i in range(2)],
            pointers=[dict(source='S0',target='S1')])
        class Backend:
            def __init__(self):self.calls=[]
            def call(self,op,inputs,*args,**kwargs):
                self.calls.append((op,deepcopy(inputs)))
                if op=='clip_text':return dict(vectors=[[1.,0.]]*6,chunk_counts=[],content_token_counts=[])
                if op=='scene_scope':return dict(data=dict(scope='global',reason='overall'))
                if op=='caption_temporal_observe':
                    c=inputs['captions'][0];state='closed' if c['id']=='C0' else 'opens'
                    return dict(data=report(row(cid=c['id'],state=state)))
                if op=='scene_correspondence_lineage':return dict(data=dict(matches=[]))
                if op=='temporal_review':return dict(data=dict(sufficient=False,reason='need context',gap='later scene',next_scene='stop',entity_ids=[]))
                if op=='temporal_qa_check':return dict(input_tokens=100)
                if op=='videotree_qa':return dict(data=dict(selected_option='0',text='box'))
                raise AssertionError('Unexpected image operation: '+op)
        cfg=load_config();cfg['query']['temporal_sparse_visual']=False
        with tempfile.TemporaryDirectory() as tmp:
            for arm in ('cluster_qa','temporal_local','temporal_cross'):
                backend=Backend()
                result=run(packet,d,[dict(path='/does/not/exist.jpg')],backend,cfg,Path(tmp)/arm,arm)
                self.assertIsNone(result['error'],result['error'])
                self.assertEqual(result['pixel_frame_count'],0)
                self.assertEqual(result['entity_source'],'summary')
                for op,inputs in backend.calls:
                    self.assertNotIn('frames',inputs)
                    if op=='caption_temporal_observe':
                        self.assertNotIn('question',inputs)
                        self.assertNotIn('options',inputs)
                        self.assertEqual(inputs['captions'],caps[:2] if inputs['captions'][0]['id']=='C0' else caps[2:])
                if arm!='cluster_qa':
                    self.assertGreater(result['caption_source_count'],0)
                    qa=next(inp for op,inp in backend.calls if op=='videotree_qa')
                    self.assertIn('not pixel verified',qa['prompts'][0])


if __name__=='__main__':unittest.main()
