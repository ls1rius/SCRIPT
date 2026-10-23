"""Default entry must use matched summary evidence and only one final QA."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import tests.test_matched_cross_stage as matched
from script_core.config import load_config,digest,base_config
from script_core.scheme4 import answer,load_source,query_fields
from script_core.state import write_json


class Scheme4Tests(unittest.TestCase):
    setUp=matched.MatchedCrossStageTests.setUp

    def test_default_cli_uses_scheme3_without_cross_stage_calls(self):
        import script_qa
        self.assertEqual(load_config()['query']['pipeline'],'scheme3')
        for name in ('script','script_local'):
            self.assertEqual(load_config(f'configs/{name}.yaml')['query']['pipeline'],'scheme3')
        self.backend.close=Mock()
        questions=self.root/'questions.jsonl'
        questions.write_text(json.dumps(self.packet['question'])+'\n')
        out=self.root/'default';uid=script_qa.safe_directory_id(self.uid)
        with patch.object(script_qa,'preflight',side_effect=lambda cfg:(cfg,{})), \
             patch.object(script_qa,'ModelWorker',return_value=self.backend), \
             patch('script_core.temporal_pipeline.run_cross_replay',side_effect=AssertionError('scheme 4 was not enabled')):
            status=script_qa.main(['query','--base',str(self.root),'--questions',str(questions),'--out',str(out)])
        self.assertEqual(status,0)
        result=json.loads((out/uid/'result.json').read_text())
        self.assertIsNone(result['error'])
        self.assertEqual(result['scheme'],3)
        self.assertEqual(result['pipeline'],'scheme3')
        self.assertEqual(result['arm'],'summary_deep')
        self.assertEqual(self.backend.answers,1)
        self.assertEqual(result['identity_comparisons'],0)
        self.assertFalse((out/uid/'cross_stage').exists())
        self.assertFalse(any(op=='scene_correspondence_lineage' for op,_ in self.backend.calls))
        prepared=json.loads((out/uid/'summary_search/result.json').read_text())
        self.assertEqual(result['answer'],prepared['answer'])
        self.assertEqual(result['pixel_frame_count'],3)
        self.assertAlmostEqual(result['cost_summary']['total'],
            prepared['cost_summary']['total']+result['directory_cost_summary']['total'])
        self.assertAlmostEqual(result['cost_summary']['initialization'],result['directory_cost_summary']['total'])
        sequence=json.loads((out/'sequence_cost.json').read_text())
        self.assertEqual(sequence['pipeline'],'scheme3')
        self.assertAlmostEqual(sequence['cumulative_total'],result['cost_summary']['total'])

    def test_scheme4_flag_uses_identical_sources_and_one_answer(self):
        import script_qa
        cfg=load_config('configs/script.yaml')
        self.assertEqual(cfg['query']['pipeline'],'scheme3')
        self.assertEqual(cfg['query']['answer_order'],'original')
        self.backend.close=Mock()
        questions=self.root/'questions.jsonl'
        questions.write_text(json.dumps(self.packet['question'])+'\n')
        out=self.root/'ordinary';uid=script_qa.safe_directory_id(self.uid)
        with patch.object(script_qa,'preflight',side_effect=lambda cfg:(cfg,{})), \
             patch.object(script_qa,'ModelWorker',return_value=self.backend), \
             patch.object(script_qa,'load_base',side_effect=AssertionError('must not reread raw video')):
            status=script_qa.main(['query','--base',str(self.root),'--questions',str(questions),'--out',str(out),'--scheme4'])
        self.assertEqual(status,0)
        result=json.loads((out/uid/'result.json').read_text())
        self.assertIsNone(result['error'])
        self.assertEqual(result['scheme'],4)
        self.assertEqual(result['answer']['selected_option'],'0')
        self.assertEqual(self.backend.answers,1)
        self.assertGreater(result['identity_comparisons'],0)
        bundle=json.loads((out/uid/'summary_search/evidence_bundle.json').read_text())
        prep=json.loads((out/uid/'summary_search/result.json').read_text())
        self.assertIsNone(prep['answer'])
        self.assertTrue(prep['evidence_only'])
        self.assertEqual(result['paired_evidence_hash'],digest(bundle))
        self.assertEqual(result['selected_seconds'],bundle['selected_seconds'])
        self.assertEqual(result['opened_scenes'],bundle['opened_scenes'])
        self.assertEqual(result['supplemental_visual']['observations'],bundle['supplemental_visual']['observations'])
        self.assertEqual(bundle['network']['identities'],[])
        self.assertEqual(result['pixel_frame_count'],0)
        self.assertEqual(result['shared_pixel_frame_count'],3)
        self.assertAlmostEqual(result['cost_summary']['initialization'],
            bundle['preparation_cost_summary']['total']+result['directory_cost_summary']['total'])
        calls=[json.loads(line) for line in (out/uid/'cross_stage/calls.jsonl').read_text().splitlines()]
        self.assertTrue(all(c['operation'] in ('scene_correspondence_lineage','caption_temporal_identity_transfer',
            'temporal_qa_context_selection','temporal_qa_check','videotree_qa','structured_output_validation') for c in calls),
            [c['operation'] for c in calls])
        self.assertTrue(all(c['input_pixels']==0 for c in calls))

    def test_pipeline_switch_cannot_reuse_other_scheme_even_after_interruption(self):
        features=[[1.,0.]]*4+[[0.,1.]]*4
        for scheme in (3,4):
            cfg=deepcopy(self.cfg);cfg['query']['pipeline']=f'scheme{scheme}'
            target=self.root/f'switch-{scheme}'
            result=answer(self.packet,features,cfg,self.backend,target)
            self.assertEqual(result['scheme'],scheme)
            self.assertEqual(result['run_config']['query']['pipeline'],f'scheme{scheme}')
            (target/'result.json').unlink()
            cfg['query']['pipeline']=f'scheme{7-scheme}'
            before=len(self.backend.calls)
            with self.assertRaisesRegex(ValueError,'different pipeline'):
                answer(self.packet,features,cfg,self.backend,target,resume=True)
            self.assertEqual(len(self.backend.calls),before)

    def test_interrupted_scheme3_search_retains_spent_budget(self):
        target=self.root/'interrupted';features=[[1.,0.]]*4+[[0.,1.]]*4
        answer(self.packet,features,self.cfg,self.backend,target)
        (target/'result.json').unlink()
        (target/'summary_search/result.json').unlink()
        before=len(self.backend.calls)
        with self.assertRaisesRegex(ValueError,'Interrupted summary search'):
            answer(self.packet,features,self.cfg,self.backend,target,resume=True)
        self.assertEqual(len(self.backend.calls),before)

    def test_scheme4_flag_rejects_legacy_config_before_preflight(self):
        import script_qa
        with patch.object(script_qa,'preflight') as preflight:
            with self.assertRaisesRegex(ValueError,'requires a scheme3/scheme4'):
                script_qa.main(['query','--base',str(self.root),'--questions','unused.jsonl',
                                '--out',str(self.root/'unused'),'--config','configs/script_coverage.yaml','--scheme4'])
        preflight.assert_not_called()

    def test_resume_reuses_completed_answer_and_rejects_changed_source_or_sample(self):
        target=self.root/'run';features=[[1.,0.]]*4+[[0.,1.]]*4
        result=answer(self.packet,features,self.cfg,self.backend,target)
        self.assertIsNone(result['error'])
        calls=len(self.backend.calls)
        self.assertEqual(answer(self.packet,features,self.cfg,self.backend,target,resume=True),result)
        self.assertEqual(len(self.backend.calls),calls)
        changed=deepcopy(self.packet);changed['question']['question']='Something different?'
        with self.assertRaisesRegex(ValueError,'identical source'):
            answer(changed,features,self.cfg,self.backend,target,resume=True)
        fid=result['supplemental_visual']['frame_ids'][0]
        frame=next(f for f in self.packet['frames'] if f['frame_id']==fid)
        Path(frame['path']).write_bytes(b'changed frame')
        with self.assertRaisesRegex(ValueError,'sampled frame changed'):
            answer(self.packet,features,self.cfg,self.backend,target,resume=True)
        self.assertEqual(len(self.backend.calls),calls)

    def test_frozen_source_preserves_original_summaries_and_accepts_new_question(self):
        row=dict(self.packet['question'],question_id='new-question',question='What color is the box?')
        packet,features=load_source(self.root,row,self.cfg)
        self.assertEqual(packet['question'],row)
        self.assertEqual(packet['captions'],self.packet['captions'])
        self.assertEqual(packet['caption_records_hash'],self.packet['caption_records_hash'])
        self.assertEqual(len(features),8)
        for extra in ('answer','selected_option','truth'):
            with self.assertRaisesRegex(ValueError,'evaluation annotations'):
                load_source(self.root,dict(row,**{extra:'0'}),self.cfg)

    def test_m0_adapter_uses_existing_summaries_and_features_without_opening_video(self):
        from script_core.video import file_hash
        root=self.root/'m0';root.mkdir()
        captions=root/'captions.json'
        write_json(captions,[dict(start=i,end=i+1,text=f'Original text {i}') for i in range(8)])
        write_json(root/'manifest.json',dict(video_id=self.uid,uri='/nonexistent/raw.mp4',duration=8,cutoff=8,
            caption_uri=str(captions),caption_hash=file_hash(captions),media_hash='no-video-read'))
        cfg=deepcopy(self.cfg);cfg['compatibility']['feature_directory']=str(root/'no-external-features')
        write_json(root/'base_hash.json',dict(base_config_hash=digest(base_config(cfg))))
        write_json(root/'feature_arrays/samples.json',dict(pts=list(range(8)),vectors=[[1.,0.]]*8))
        packet,features=load_source(root,self.packet['question'],cfg)
        self.assertEqual([c['text'] for c in packet['captions']],[f'Original text {i}' for i in range(8)])
        self.assertEqual(features,[[1.,0.]]*8)
        self.assertEqual(packet['frames'],[])
        manifest=json.loads((root/'manifest.json').read_text());manifest.update(duration=9,cutoff=9)
        write_json(root/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'complete legal video timeline'):
            load_source(root,self.packet['question'],cfg)

    def test_invalid_budget_or_foreign_output_does_not_start_inference(self):
        for cap in (True,float('nan'),float('inf'),1):
            with self.assertRaisesRegex(ValueError,'Total cap'):
                answer(self.packet,[],self.cfg,self.backend,self.root/'invalid',total_cap=cap)
        target=self.root/'old';write_json(target/'result.json',{'legacy':True})
        with self.assertRaisesRegex(ValueError,'different pipeline'):
            answer(self.packet,[],self.cfg,self.backend,target)
        self.assertEqual(json.loads((target/'result.json').read_text()),{'legacy':True})
        self.assertEqual(self.backend.calls,[])

    def test_legacy_profiles_and_debug_entry_select_the_intended_pipeline(self):
        for name in ('coverage','guided','scene_lazy','key_memory','videotree'):
            self.assertEqual(load_config(f'configs/script_{name}.yaml')['query']['pipeline'],'legacy')
        launch=json.loads('\n'.join(line for line in Path('.vscode/launch.json').read_text().splitlines()
                                    if not line.lstrip().startswith('//')))
        args=launch['configurations'][0]['args']
        self.assertEqual(args[args.index('--config')+1],'${workspaceFolder}/configs/script.yaml')
        self.assertTrue(args[args.index('--out')+1].endswith('/scheme3'))
        self.assertNotIn('--scheme4',args)

    def test_local_smoke_forwards_scheme4_switch_and_uses_separate_outputs(self):
        from script.run_local_four_arm import ordinary_query,ordinary_scheme4
        import script_qa
        with patch.object(script_qa,'main',return_value=0) as query_main:
            ordinary_query(self.root)
            args=query_main.call_args.args[0]
            self.assertNotIn('--scheme4',args)
            self.assertEqual(args[args.index('--out')+1],str(self.root/'scheme3'))
            ordinary_scheme4(self.root)
            args=query_main.call_args.args[0]
            self.assertIn('--scheme4',args)
            self.assertEqual(args[args.index('--out')+1],str(self.root/'scheme4'))


if __name__=='__main__':unittest.main()
