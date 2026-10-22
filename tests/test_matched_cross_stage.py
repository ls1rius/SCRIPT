"""Four-arm controls: cross-stage memory changes while source evidence is fixed."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from script.run_temporal_entity import execute,gate,ARMS
from script_core.config import load_config,digest
from script_core.temporal_pipeline import run_cross_replay
from tests.test_caption_temporal import row,report


class Backend:
    def __init__(self):self.calls=[];self.answers=0
    def call(self,op,inp,*args):
        self.calls.append((op,deepcopy(inp)))
        if op=='temporal_summary':return dict(data=dict(description='C holds a box'))
        if op=='clip_text':return dict(vectors=[[1.,0.]]*6,chunk_counts=[],content_token_counts=[])
        if op=='scene_scope':return dict(data=dict(scope='global',reason='whole video'))
        if op=='caption_temporal_observe':return dict(data=report(row(cid=inp['captions'][0]['id'],state='holds')))
        if op=='temporal_review':return dict(data=dict(sufficient=False,reason='missing color',gap='box color missing',next_scene='stop',entity_ids=[]))
        if op=='sparse_visual_plan':return dict(data=dict(needed=True,reason='visible color distinguishes options',checks=[dict(scene_id='S0',caption_ids=['C1'],target='box color',missing_detail='red or blue',option_ids=['0','1'])]))
        if op=='sparse_visual_observe':return dict(data=dict(description='The box is red.',kind='state',source_frame_ids=[f['frame_id'] for f in inp['frames']],unresolved=''))
        if op=='sparse_visual_verify':return dict(data=dict(result='supported',whole_claim_supported=True,source_frame_ids=[f['frame_id'] for f in inp['frames']],evidence='Red surface visible.',unresolved=''))
        if op=='scene_correspondence_lineage':
            left=inp['left']['entities'][0];right=inp['right']['entities'][0]
            return dict(data=dict(matches=[dict(left=dict(entity=left['id'],caption_ids=left['caption_ids'][:1]),
                right=dict(entity=right['id'],caption_ids=right['caption_ids'][:1]),relation='same_instance',reason='text inference')]))
        if op=='temporal_qa_check':return dict(input_tokens=500)
        if op=='videotree_qa':
            answer=str(self.answers);self.answers+=1
            return dict(data=dict(selected_option=answer,text='independent final QA'))
        raise AssertionError(op)


class MatchedCrossStageTests(unittest.TestCase):
    def setUp(self):
        import torch
        from PIL import Image
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.cfg=load_config();self.cfg['query']['temporal_sparse_visual']=True
        self.uid='test-video';frames=[]
        for i in range(8):
            p=self.root/f'{i}.jpg';Image.new('RGB',(64,64),(200,40,30)).save(p)
            frames.append(dict(frame_id=f'F{i}',pts=float(i),path=str(p),width=64,height=64,sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
        feature=self.root/'features.pt';torch.save(torch.tensor([[1.,0.]]*4+[[0.,1.]]*4),feature)
        self.packet=dict(question=dict(question_id=self.uid,video_id=self.uid,question='What is the overall activity?',options=['red','blue','green','black','white']),
            frames=frames,feature_uri=str(feature),feature_hash=hashlib.sha256(feature.read_bytes()).hexdigest(),
            captions=[dict(id=f'C{i}',interval=[i,i+1],text='C holds the box.') for i in range(8)])
        self.packet['caption_records_hash']=digest(self.packet['captions'])
        p=self.root/'inputs'/f'{self.uid}.json';p.parent.mkdir();p.write_text(json.dumps(self.packet))
        (self.root/'input_hashes.json').write_text(json.dumps({f'inputs/{self.uid}.json':hashlib.sha256(p.read_bytes()).hexdigest()}))
        self.backend=Backend()

    def build_deep(self):
        r=execute(self.root,'summary_deep',self.uid,self.backend,self.cfg)
        self.assertIsNone(r['error'],r['error'])
        source=self.root/'arms/summary_deep/runs'/self.uid
        return r,json.loads((source/'evidence_bundle.json').read_text()),source

    def test_four_arm_order_separates_deep_search_from_cross_stage_memory(self):
        self.assertEqual(list(ARMS),['cluster_qa','temporal_local','summary_deep','temporal_cross'])

    def test_cross_replay_changes_memory_with_exact_same_summaries_and_visual_facts(self):
        deep,bundle,source=self.build_deep();start=len(self.backend.calls)
        deep_net=json.loads((source/'network.json').read_text())
        self.assertEqual(deep_net['identities'],[])
        self.assertEqual(deep_net['aliases'],{})
        self.assertTrue(all(len(e['scene_ids'])==1 for e in deep_net['entities'].values()))
        self.assertFalse({'answer','prediction','truth','selected_option'}&bundle.keys())
        cross=execute(self.root,'temporal_cross',self.uid,self.backend,self.cfg)
        self.assertIsNone(cross['error'],cross['error'])
        self.assertEqual(deep['opened_scenes'],cross['opened_scenes'])
        self.assertEqual(deep['selected_seconds'],cross['selected_seconds'])
        self.assertEqual(deep['supplemental_visual']['observations'],cross['supplemental_visual']['observations'])
        self.assertEqual(deep['answer']['selected_option'],'0')
        self.assertEqual(cross['answer']['selected_option'],'1')
        self.assertGreater(cross['identity_comparisons'],0)
        self.assertLess(cross['entity_count'],deep['entity_count'])
        self.assertEqual(cross['pixel_frame_count'],0)
        self.assertEqual(cross['shared_pixel_frame_count'],deep['pixel_frame_count'])
        self.assertEqual(cross['cost_summary']['initialization'],bundle['preparation_cost_summary']['total'])
        self.assertGreaterEqual(cross['cost_summary']['total'],cross['cost_summary']['initialization'])
        self.assertEqual(cross['paired_evidence_hash'],digest(bundle))
        new_calls=self.backend.calls[start:]
        self.assertTrue(all(op in ('scene_correspondence_lineage','temporal_qa_check','videotree_qa') for op,_ in new_calls))
        self.assertTrue(all(not inp.get('frames') for _,inp in new_calls))
        for op,inp in new_calls:
            if op=='videotree_qa':self.assertIn('The box is red.',inp['prompts'][0])

    def test_input_mismatch_and_prediction_contamination_are_rejected(self):
        _,bundle,_=self.build_deep()
        for changed in [dict(bundle,packet_hash='wrong'),dict(bundle,answer=dict(selected_option='4'))]:
            with self.assertRaises(ValueError):run_cross_replay(self.packet,changed,self.backend,self.cfg,self.root/'rejected')

    def test_premerged_source_network_is_not_an_independent_scene_control(self):
        _,bundle,_=self.build_deep()
        bundle['network']['aliases']['E1']='E0'
        with self.assertRaises(ValueError):run_cross_replay(self.packet,bundle,self.backend,self.cfg,self.root/'rejected')

    def test_four_arm_gate_rejects_changed_evidence_and_unpaid_shared_cost(self):
        (self.root/'config.json').write_text(json.dumps(self.cfg))
        (self.root/'smoke').mkdir()
        (self.root/'smoke/ids.json').write_text(json.dumps([self.uid]))
        for arm in ARMS:
            r=execute(self.root,arm,self.uid,self.backend,self.cfg,smoke=True)
            self.assertIsNone(r['error'],r['error'])
        probe=self.root/'verification/sparse-visual-probe/status.json';probe.parent.mkdir(parents=True)
        probe.write_text(json.dumps(dict(passed=True,selected_frame_ids=['F0','F1','F2'],cost_summary=dict(budget_violation=False))))
        self.assertTrue(gate(self.root))
        path=self.root/'smoke/arms/temporal_cross/runs'/self.uid/'result.json'
        original=json.loads(path.read_text())
        changed=deepcopy(original);changed['selected_seconds']=changed['selected_seconds'][:4]
        path.write_text(json.dumps(changed));self.assertFalse(gate(self.root))
        changed=deepcopy(original);changed['cost_summary']['initialization']+=1.
        path.write_text(json.dumps(changed));self.assertFalse(gate(self.root))


if __name__=='__main__':unittest.main()
