"""Summary-first routing, strict visual limits and independent verification."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script_core.budget import BudgetLedger,Meter
from script_core.config import load_config
from script_core import sparse_visual as sparse
from script_core.schemas import output_schema,validate_output


class Backend:
    def __init__(self,needed=True,kind='state',supported=True,whole=True,support_count=3):
        self.seen=[];self.needed=needed;self.kind=kind;self.supported=supported
        self.whole=whole;self.support_count=support_count
    def call(self,op,inputs,*args):
        self.seen.append((op,deepcopy(inputs)))
        refs=[f['frame_id'] for f in inputs.get('frames',[])]
        if op=='sparse_visual_plan':
            value=dict(needed=self.needed,reason='color omitted',checks=[dict(scene_id='S0',caption_ids=['C1'],
                target='Check the box color',missing_detail='red or blue',option_ids=['0','1'])] if self.needed else [])
        elif op=='sparse_visual_observe':
            value=dict(description='A red box is visible.',kind=self.kind,source_frame_ids=refs,unresolved='')
        elif op=='sparse_visual_verify':
            value=dict(result='supported' if self.supported else 'unresolved',whole_claim_supported=self.whole,
                source_frame_ids=refs[:self.support_count],evidence='The sampled object is red.',unresolved='')
        else:raise AssertionError(op)
        return dict(data=value)


class SparseVisualTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.frames=[]
        for i in range(10):
            path=self.root/f'{i}.jpg';Image.new('RGB',(64,64),(200,40,30)).save(path)
            self.frames.append(dict(frame_id=f'F{i}',pts=float(i),path=str(path),width=64,height=64,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        self.caps=[dict(id=f'C{i}',interval=[i,i+1],text='C holds the box.') for i in range(10)]
        self.directory=dict(captions=self.caps,scenes=[dict(id='S0',interval=[0,5],caption_ids=[f'C{i}' for i in range(5)]),
            dict(id='S1',interval=[5,10],caption_ids=[f'C{i}' for i in range(5,10)])])
        self.query=dict(question='What color is the box?',options=['red','blue','green','white','black'])
        self.review=[dict(sufficient=False,gap='box color is missing',coverage_check=dict(met=True))]
        self.cfg=load_config();self.ledger=BudgetLedger(200.,answer_reserve=30.)
    def run_check(self,backend=None,reviews=None,scope=None,frames=None):
        backend=backend or Backend()
        meter=Meter(backend,self.ledger,self.cfg,self.root/'raw')
        try:
            value=sparse.supplement(self.query,self.directory,self.frames if frames is None else frames,['S0'],
                self.review if reviews is None else reviews,scope or dict(scope='local'),meter,self.root)
        finally:meter.close()
        return value,backend

    def test_sufficient_text_and_missing_concrete_gap_never_access_images(self):
        for review in [dict(sufficient=True,gap=''),dict(sufficient=False,gap='')]:
            with patch.object(sparse,'checked_frames',side_effect=AssertionError('images accessed')):
                value,backend=self.run_check(reviews=[review])
            self.assertFalse(value['triggered']);self.assertEqual(backend.seen,[])

    def test_exhaustive_coverage_gap_does_not_trigger_sparse_images(self):
        review=[dict(sufficient=False,gap='need every occurrence',coverage_check=dict(met=False))]
        value,backend=self.run_check(reviews=review,scope=dict(scope='exhaustive'))
        self.assertFalse(value['triggered']);self.assertEqual(backend.seen,[])

    def test_planner_can_abstain_without_reading_any_image(self):
        with patch.object(sparse,'checked_frames',side_effect=AssertionError('images accessed')):
            value,backend=self.run_check(Backend(needed=False))
        self.assertFalse(value['triggered'])
        self.assertEqual([op for op,_ in backend.seen],['sparse_visual_plan'])
        self.assertNotIn('frames',backend.seen[0][1])

    def test_one_three_frame_check_and_question_blind_verification(self):
        value,backend=self.run_check()
        self.assertEqual(value['selected_frame_ids'],['F0','F1','F2'])
        self.assertEqual(len(value['rounds']),1)
        self.assertEqual(len(value['observations']),1)
        self.assertEqual([op for op,_ in backend.seen],['sparse_visual_plan','sparse_visual_observe','sparse_visual_verify'])
        verify=backend.seen[-1][1]
        self.assertEqual(set(verify),{'frames','claim'})
        self.assertLessEqual(value['seconds'],60.)
        self.assertIsNone(self.ledger.task_cap)
        self.assertFalse(self.ledger.budget_violation)

    def test_partial_and_unresolved_claims_are_not_used_for_answering(self):
        for backend in [Backend(supported=False),Backend(whole=False)]:
            value,_=self.run_check(backend)
            self.assertEqual(value['observations'],[])

    def test_action_needs_three_distinct_supporting_times(self):
        value,_=self.run_check(Backend(kind='action',support_count=1))
        self.assertEqual(value['observations'],[])
        self.assertIn('process evidence',value['rounds'][0]['rejection'])
        value,_=self.run_check(Backend(kind='action',support_count=3))
        self.assertEqual(len(value['observations']),1)

    def test_nonlocal_or_unopened_targets_are_rejected_before_loading(self):
        for check in [dict(scene_id='S1',caption_ids=['C6']),dict(scene_id='S0',caption_ids=['C9'])]:
            with self.assertRaises(ValueError):sparse.select_frames(check,self.directory,self.frames,['S0'])

    def test_selection_never_exceeds_three_frames_or_reuses_a_frame(self):
        check=dict(scene_id='S0',caption_ids=['C0','C4'])
        frames=sparse.select_frames(check,self.directory,self.frames,['S0'],used=['F0'])
        self.assertLessEqual(len(frames),3)
        self.assertNotIn('F0',[f['frame_id'] for f in frames])
        self.assertTrue(all(f['pts']<5 for f in frames))

    def test_black_frames_are_rejected_without_scanning_replacement_images(self):
        from PIL import Image
        for f in self.frames[:3]:
            Image.new('RGB',(64,64),(0,0,0)).save(f['path'])
            f['sha256']=hashlib.sha256(Path(f['path']).read_bytes()).hexdigest()
        value,backend=self.run_check()
        self.assertFalse(value['triggered'])
        self.assertEqual(value['invalid_frame_ids'],['F0','F1','F2'])
        self.assertEqual([op for op,_ in backend.seen],['sparse_visual_plan'])

    def test_changed_source_is_not_sent_to_the_model(self):
        Path(self.frames[0]['path']).write_bytes(b'changed')
        value,backend=self.run_check()
        self.assertFalse(value['triggered'])
        self.assertEqual([op for op,_ in backend.seen],['sparse_visual_plan'])
        self.assertTrue(value['errors'])

    def test_insufficient_budget_does_not_start_a_visual_check(self):
        self.ledger.actual=160.
        value,backend=self.run_check()
        self.assertEqual(backend.seen,[])
        self.assertFalse(value['triggered'])

    def test_live_schemas_reject_foreign_image_and_summary_references(self):
        inp=dict(captions=self.caps[:5],opened_scenes=['S0'],options=self.query['options'])
        schema=output_schema('sparse_visual_plan',inp)
        invalid=dict(needed=True,reason='missing',checks=[dict(scene_id='S0',caption_ids=['C9'],
            target='color',missing_detail='red vs blue',option_ids=['0','1'])])
        with self.assertRaises(ValueError):validate_output(invalid,schema)
        schema=output_schema('sparse_visual_observe',dict(frames=self.frames[:3]))
        with self.assertRaises(ValueError):validate_output(dict(description='x',kind='state',source_frame_ids=['F9'],unresolved=''),schema)

    def test_only_third_arm_can_read_images_and_text_entity_network_stays_text(self):
        from script_core.temporal_pipeline import run
        from tests.test_caption_temporal import row,report
        directory=dict(captions=self.caps[:4],scenes=[dict(id='S0',interval=[0,4],center=[1.,0.],
            chronological_order=list(range(4)),caption_ids=[f'C{i}' for i in range(4)],description='C holds a box')],pointers=[])
        packet=dict(question=dict(self.query,question_id='q',video_id='v'))
        class PipelineBackend(Backend):
            def call(self,op,inputs,*args):
                if op.startswith('sparse_visual_'):return super().call(op,inputs,*args)
                self.seen.append((op,deepcopy(inputs)))
                if op=='clip_text':return dict(vectors=[[1.,0.]]*6,chunk_counts=[],content_token_counts=[])
                if op=='scene_scope':return dict(data=dict(scope='local',reason='color'))
                if op=='caption_temporal_observe':return dict(data=report(row(state='holds')))
                if op=='temporal_review':return dict(data=dict(sufficient=False,reason='missing color',gap='box color is missing',next_scene='stop',entity_ids=[]))
                if op=='temporal_qa_check':return dict(input_tokens=200)
                if op=='videotree_qa':return dict(data=dict(selected_option='0',text='red'))
                raise AssertionError(op)
        cfg=load_config();cfg['query']['temporal_sparse_visual']=True
        for arm in ('cluster_qa','temporal_local','temporal_cross'):
            with self.subTest(arm=arm):
                backend=PipelineBackend();target=self.root/arm
                result=run(packet,directory,self.frames,backend,cfg,target,arm)
                self.assertIsNone(result['error'],result['error'])
                self.assertEqual(result['pixel_frame_count'],3 if arm=='temporal_cross' else 0)
                self.assertEqual(result['supplemental_visual']['enabled'],arm=='temporal_cross')
                image_ops=[op for op,inp in backend.seen if inp.get('frames')]
                self.assertEqual(image_ops,['sparse_visual_observe','sparse_visual_verify'] if arm=='temporal_cross' else [])
                self.assertNotIn('source_frame_ids',(target/'network.json').read_text())
                if arm=='temporal_cross':
                    qa=next(inp for op,inp in backend.seen if op=='videotree_qa')
                    self.assertIn('A red box is visible.',qa['prompts'][0])
                    self.assertIn('C holds the box.',qa['prompts'][0])
                    self.assertEqual(result['supplemental_visual']['rounds'][0]['check']['option_ids'],['0','1'])


if __name__=='__main__':unittest.main()
