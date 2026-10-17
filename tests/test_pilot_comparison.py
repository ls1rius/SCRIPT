"""Shared-caption provenance, real batching semantics and paired pilot accounting."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import torch

from script_core.backend import LocalModels
from script_core.build import build_video, load_base
from script_core.config import DEFAULTS
from script_core.video import file_hash, load_captions
from test_script_core import fixture_memory

spec = importlib.util.spec_from_file_location('pilot', Path(__file__).resolve().parents[1]/'script/run_pilot_comparison.py')
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


class CaptionTests(unittest.TestCase):
    def test_caption_prefix_rejects_text_generated_across_the_cutoff(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'captions.json'
            source.write_text(json.dumps([{'start': 0, 'end': 1, 'text': 'allowed'},
                                          {'start': 1, 'end': 2, 'text': 'contains future'}]))
            manifest = {'caption_uri': str(source), 'caption_hash': file_hash(source), 'cutoff': 1.5, 'duration': 2}
            self.assertEqual([s['text'] for s in load_captions(manifest)['data']], ['allowed'])
            source.write_text('[]')
            with self.assertRaisesRegex(ValueError, 'intact'):
                load_captions(manifest)

    def test_imported_captions_do_not_fabricate_pixels_or_verified_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            video, captions = directory/'video.mp4', directory/'captions.json'
            video.write_bytes(b'fixture source identity; decode is supplied by the component fixture')
            captions.write_text(json.dumps({'producer': {'name': 'LaViLa'}, 'spans': [
                {'start': i, 'end': i+1, 'text': f'caption {i}'} for i in range(12)]}))
            manifest = deepcopy(fixture_memory().manifest)
            manifest.update(uri=str(video), media_hash=file_hash(video), caption_uri=str(captions), caption_hash=file_hash(captions))
            operations = []
            class Backend:
                def call(self, operation, inputs, timeout):
                    operations.append(operation)
                    if operation == 'inspect': return {'data': manifest}
                    if operation == 'decode':
                        return {'data': [dict(f, width=64, height=48, crop=None, path='fixture.png')
                                        for f in manifest['frames'] if inputs['interval'][0] <= f['pts'] < inputs['interval'][1]]}
                    if operation == 'features': return {'vectors': [[1., 0.] for _ in inputs['frames']]}
                    if operation == 'embed': return {'vectors': [[1., 0.] for _ in inputs['texts']]}
                    if operation == 'summarize': return {'data': {'text': 'Provided coarse descriptions', 'place': None,
                                                                   'event_keywords': [], 'uncertainties': []}}
                    return LocalModels(DEFAULTS).call(operation, inputs)
            cfg = deepcopy(DEFAULTS)
            cfg['initialization']['caption_source'] = 'provided'
            memory, metadata = build_video({'video_id': 'v', 'uri': str(video), 'caption_uri': str(captions)},
                                          cfg, Backend(), directory/'base')
            self.assertNotIn('caption', operations)
            self.assertEqual(operations.count('import_captions'), 1)
            self.assertTrue(all(not e['frame_inputs'] and e['source_type'] == 'caption' for e in memory.tables['evidence'].values()))
            self.assertTrue(all(c['level'] == 'sampled' and c['result'] == 'unknown' for c in memory.tables['coverage'].values()))
            self.assertFalse(memory.tables['claims'])
            self.assertFalse(memory.tables['entities'])
            memory.validate()
            load_base(directory/'base')
            captions.write_text('[]')
            with self.assertRaisesRegex(ValueError, 'Caption content changed'):
                load_base(directory/'base')


class BatchingTests(unittest.TestCase):
    def test_visual_batch_64_keeps_each_frame_in_order_and_the_remainder(self):
        import numpy as np
        from PIL import Image
        class Processor:
            def __init__(self): self.sizes = []
            def __call__(self, images, return_tensors, **kwargs):
                self.sizes.append(len(images))
                return SimpleNamespace(pixel_values=torch.stack([
                    torch.tensor(np.asarray(image).copy()).permute(2, 0, 1).float() for image in images]))
        class Model:
            device, dtype = 'cpu', torch.float32
            def encode_image(self, pixels): return pixels.mean(dim=(-1, -2))
        with tempfile.TemporaryDirectory() as folder:
            frames = []
            for i in range(65):
                path = Path(folder)/f'{i}.png'
                Image.new('RGB', (8, 8), (i+1, 70, 120)).save(path)
                frames.append({'path': str(path)})
            cfg = deepcopy(DEFAULTS)
            cfg['models']['batching']['visual_encoder'] = 64
            batched = LocalModels(cfg)
            processor = Processor()
            batched.loaded['visual_encoder'] = Model(), processor
            result = batched.features(frames)
            self.assertEqual(processor.sizes, [64, 1])
            cfg = deepcopy(cfg)
            cfg['models']['batching']['visual_encoder'] = 1
            serial = LocalModels(cfg)
            serial.loaded['visual_encoder'] = Model(), Processor()
            self.assertEqual(result, serial.features(frames))

    def test_embeddings_preserve_order_masks_and_token_budget(self):
        class Encoding(dict):
            def __getattr__(self, key): return self[key]
            def to(self, device): return self
        class Tokenizer:
            def __call__(self, texts, padding=False, truncation=False, return_tensors=None):
                ids = [[ord(t[0]), *[1]*len(t), 2] for t in texts]
                if not return_tensors: return {'input_ids': ids}
                length = max(map(len, ids))
                return Encoding(input_ids=torch.tensor([x+[0]*(length-len(x)) for x in ids]),
                                attention_mask=torch.tensor([[1]*len(x)+[0]*(length-len(x)) for x in ids]))
        class Model:
            device = 'cpu'
            def __init__(self): self.shapes = []
            def __call__(self, input_ids, attention_mask):
                self.shapes.append(tuple(input_ids.shape))
                values = torch.stack([input_ids[:, 0].float(), attention_mask.sum(-1).float()], -1)
                return SimpleNamespace(last_hidden_state=values[:, None, :])
        cfg = deepcopy(DEFAULTS)
        cfg['models']['batching'].update(text_embedding=8, text_embedding_max_tokens=8)
        batched = LocalModels(cfg)
        model = Model()
        batched.loaded['text_embedding'] = model, Tokenizer()
        result = batched.embed(['a', 'b', 'ccccc'])
        self.assertEqual(model.shapes, [(2, 3), (1, 7)])
        self.assertEqual(batched.model_tokens['text_embedding'], 13)
        cfg = deepcopy(cfg)
        cfg['models']['batching']['text_embedding'] = 1
        serial = LocalModels(cfg)
        serial.loaded['text_embedding'] = Model(), Tokenizer()
        self.assertEqual(result, serial.embed(['a', 'b', 'ccccc']))
        with self.assertRaisesRegex(ValueError, '8192'):
            batched.embed(['a'*8191])


class PilotTests(unittest.TestCase):
    def test_fixed_samples_shared_captions_and_labels_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root/'data'
            (data/'videos').mkdir(parents=True)
            annotations = {f'v{i}': {'question': 'Question', **{f'option {j}': str(j) for j in range(5)}, 'truth': i%5} for i in range(4)}
            pilot.write(data/'subset_anno.json', annotations)
            pilot.write(data/'lavila_subset.json', {uid: ['shared description'] for uid in annotations})
            for uid in annotations: (data/'videos'/f'{uid}.mp4').write_bytes(b'fixture')
            first = pilot.prepare(root/'first', 4, 2, 0, data)
            second = pilot.prepare(root/'second', 4, 2, 0, data)
            self.assertEqual(first['video_ids'], second['video_ids'])
            self.assertEqual(first['shared_caption_sha256'], second['shared_caption_sha256'])
            concurrent = pilot.prepare(root/'concurrent', 4, 4, 0, data,
                                       feature_batch_size=64, embedding_batch_size=16, streams_per_gpu=2)
            self.assertEqual(concurrent['video_ids'], first['video_ids'])
            self.assertEqual(concurrent['gpu_jobs'], 2)
            self.assertEqual(concurrent['streams_per_gpu'], 2)
            self.assertEqual(concurrent['feature_batch_size'], 64)
            inputs = (root/'first/shards/0/questions.jsonl').read_text()+(root/'first/shards/0/videotree_input.json').read_text()
            self.assertNotIn('truth', inputs)
            self.assertNotIn('selected_option', inputs)
            for row in pilot.read(root/'first/shards/0/manifest.json'):
                self.assertEqual(pilot.read(row['caption_uri'])['spans'][0]['text'], 'shared description')
            with self.assertRaisesRegex(ValueError, 'immutable'):
                pilot.prepare(root/'first', 4, 2, 0, data)
            report = pilot.score(root/'first')
            self.assertFalse(report['complete'])
            self.assertIsNone(report['groups']['SCRIPT']['final_accuracy'])
            for i in range(2):
                directory = root/'first'/'shards'/str(i)
                index = {}
                for row in pilot.read(directory/'manifest.json'):
                    uid = row['video_id']
                    base = directory/'script_base'/uid
                    pilot.write(base/'base_hash.json', {'base_hash': uid})
                    index[uid] = str(base)
                    pilot.write(directory/'videotree_runs'/uid/'summary.json',
                        {'examples': [{'answer': 'A', 'selected_frames': 1}], 'seconds': 1.})
                pilot.write(directory/'script_base'/'videos.json', index)
            reused = pilot.prepare(root/'reused', 4, 2, 0, data, reuse_from=root/'first')
            self.assertEqual(reused['video_ids'], first['video_ids'])
            self.assertEqual(pilot.score(root/'reused')['groups']['VideoTree']['completed_questions'], 4)
            pilot.verify_snapshot(root/'reused')
            baseline = next((root/'first'/'shards'/'0'/'videotree_runs').glob('*/summary.json'))
            original_baseline = baseline.read_bytes()
            baseline.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Reused experiment input changed'):
                pilot.verify_snapshot(root/'reused')
            baseline.write_bytes(original_baseline)
            pilot.write(root/'first/shards/0/script_build_errors.json',
                        {uid: {'code': 'build_failed'} for uid in pilot.read(root/'first/shards/0/videotree_input.json')})
            report = pilot.score(root/'first')
            self.assertEqual(report['groups']['SCRIPT']['completed_questions'], 2)
            self.assertEqual(report['groups']['SCRIPT']['errors'], 2)
            self.assertEqual(report['groups']['SCRIPT']['accuracy_on_completed'], 0)
            self.assertIsNone(report['groups']['SCRIPT']['final_accuracy'])


if __name__ == '__main__':
    unittest.main()
