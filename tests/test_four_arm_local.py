"""Shared input publication, isolated paired evidence and spent-budget protection."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from script import run_four_arm_300_local as local


class LocalFourArmTests(unittest.TestCase):
    def test_fixed_cohort_is_distinct_order_independent_and_label_independent(self):
        ids=[f'v{i}' for i in range(498)]+[
            '0074f737-11cb-497d-8d07-77c3a8127391','0a01d7d0-11d6-4af6-abd9-2025656d3c63']
        first=local.cohort_ids({u:dict(truth=0) for u in ids})
        second=local.cohort_ids({u:dict(truth=4) for u in reversed(ids)})
        self.assertEqual(first,second)
        self.assertEqual(len(first),300)
        self.assertEqual(len(set(first)),300)
        self.assertNotIn(ids[-1],first[:100])
        self.assertNotIn(ids[-2],first[:100])

    def test_ready_packet_has_exact_caption_sources_and_matching_hashes(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'out';out.mkdir()
            model=root/'model';model.mkdir()
            feature=root/'data/EgoSchema/egoschema_features/v.pt';feature.parent.mkdir(parents=True)
            feature.write_bytes(b'feature fixture')
            local.write(feature.with_suffix('.json'),dict(model=str(model),shape=[2,1280],fps=1,frame_indices=[0,1]))
            for i in range(2):
                path=root/f'data/EgoSchema/egoschema_frames/v/{i}.jpg';path.parent.mkdir(parents=True,exist_ok=True)
                Image.new('RGB',(16,16),(255,0,0)).save(path)
            local.write(out/'input_hashes.json',{})
            captions=[dict(id=f'C{i}',interval=[i,i+1],text='C holds a cup.')for i in range(2)]
            question=dict(question_id='v',video_id='v',question='What is held?',options=['cup','a','b','c','d'])
            with patch.object(local,'ROOT',root):
                local.make_packet(out,'v',question,captions,dict(models=dict(visual_encoder=str(model))))
            packet=local.read(out/'inputs/v.json');ready=local.read(out/'input_ready/v.json')
            self.assertEqual(packet['captions'],captions)
            self.assertEqual(packet['question'],question)
            self.assertNotIn('truth',json.dumps(packet))
            self.assertEqual(ready['packet_hash'],local.sha(out/'inputs/v.json'))
            self.assertEqual(ready['packet_hash'],local.read(out/'input_hashes.json')['inputs/v.json'])
            self.assertTrue(ready['ready'])

    def test_paired_evidence_is_ready_before_source_answer_and_does_not_read_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);local.write(out/'arms/summary_deep/runs/v/evidence_bundle.json',dict(network={}))
            with patch.object(local,'read',side_effect=AssertionError('Do not read a source answer')):
                self.assertTrue(local.paired_ready(out,'v'))

    def test_failed_source_without_bundle_unblocks_dependent_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);local.write(out/'arms/summary_deep/runs/v/result.json',dict(error=dict(message='failed')))
            self.assertFalse(local.paired_ready(out,'v'))

    def test_occupied_gpu_is_rejected_without_running_a_mutation(self):
        with patch.object(local,'gpu_inventory',return_value={'2':dict(uuid='gpu',used_mib=1000,utilization=0)}):
            with self.assertRaisesRegex(ValueError,'occupied'):local.require_idle(['2'])
        with self.assertRaisesRegex(ValueError,'distinct'):local.require_idle(['2','2'])

    def test_failed_asset_worker_does_not_leave_input_waiter_hanging(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);local.write(out/'workers/assets.json',dict(state='failed'))
            with self.assertRaisesRegex(RuntimeError,'Asset preparation worker failed'):
                local.wait_ready(out,'v','cluster_qa')

    def test_interrupted_query_never_restarts_and_resets_spent_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);local.write(out/'cohort.json',['v'])
            target=out/'arms/cluster_qa/runs/v';target.mkdir(parents=True)
            (target/'calls.jsonl').write_text('{}\n')
            with patch.object(local,'verify_snapshot'),patch.object(local,'load_config',return_value={}),\
                 patch('script_core.backend.preflight',return_value=({},{})),\
                 patch('script_core.budget.ModelWorker') as backend,\
                 patch.object(local,'wait_ready',return_value=dict(ready=True)),\
                 patch.object(local,'execute') as execute:
                with self.assertRaisesRegex(RuntimeError,'retains spent budget'):
                    local.run_arm(out,'cluster_qa')
                execute.assert_not_called()
                backend.return_value.close.assert_called_once_with(graceful=True)


if __name__=='__main__':unittest.main()
