"""Real local EVA, Grounding DINO, Qwen-VL and BGE-M3 adapters. No mock fallback."""
import json
from pathlib import Path
import time

from .prompts import CONTRACTS, SYSTEM

DETECTION_TERMS = ('person bag backpack handbag briefcase envelope folder document book box bottle cup bowl plate '
                   'phone laptop keyboard mouse monitor camera keys pen scissors tool chair sofa table desk '
                   'cabinet shelf drawer door window bed refrigerator sink vehicle bicycle dog cat').split()


class SoftDeadlineExceeded(TimeoutError):
    """Cooperative cancellation completed; the loaded worker remains usable."""


def deadline_stopping_criteria(deadline):
    from transformers import StoppingCriteria, StoppingCriteriaList
    class Deadline(StoppingCriteria):
        def __call__(self, input_ids, scores, **kwargs):
            return time.monotonic() >= deadline
    return StoppingCriteriaList([Deadline()])


class LocalModels:
    def __init__(self, cfg):
        self.cfg = cfg
        self.loaded = {}
        self.enforcer_data = None
        self.gpu_acquired_at = None
        self.model_pixels = {}
        self.model_tokens = {}
        self.deadline = None

    def check_deadline(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SoftDeadlineExceeded('Processing deadline reached; worker retained')

    def call(self, operation, inputs):
        self.check_deadline()
        if operation == 'prepare_vlm':
            phases = self.prepare_vlm()
            return {'ready': True, 'preparation': phases}
        if operation == 'import_captions':
            from .video import load_captions
            return load_captions(inputs['manifest'])
        if operation == 'impute_features':
            from .segmentation import impute_short_samples
            return {'data': impute_short_samples(**inputs)}
        if operation == 'segment':
            from .segmentation import contiguous_partition, frame_boundaries
            cfg = inputs['cfg']
            if cfg['solver'] in ('fixed_windows', 'kmeans_runs'):
                from .segmentation import comparison_partition
                boundaries, metadata = comparison_partition(inputs['features'], inputs['sample_pts'],
                    inputs['frame_pts'], inputs['cutoff'], cfg, self.cfg['seed'])
                return {'data': boundaries, 'metadata': metadata}
            segments = contiguous_partition(inputs['features'], cfg['penalty'], cfg['min_samples'], cfg['tie_epsilon'])
            return {'data': frame_boundaries(segments, inputs['sample_pts'], inputs['frame_pts'], inputs['cutoff'])}
        if operation == 'shot_partition':
            from .video import shot_boundaries
            return shot_boundaries(**inputs)
        if operation == 'track':
            from .tracking import track_detections
            return {'data': track_detections(inputs['detections'], inputs['cfg'])}
        if operation == 'inspect':
            from .video import inspect_video
            return {'data': inspect_video(inputs['item'], inputs['base_config'])}
        if operation == 'decode':
            from .video import read_frames
            return {'data': read_frames(**inputs)}
        if operation == 'features':
            return {'vectors': self.features(inputs['frames'])}
        if operation == 'embed':
            return {'vectors': self.embed(inputs['texts'])}
        if operation == 'detect':
            return {'detections': self.detect(inputs['frames'], inputs.get('terms', DETECTION_TERMS), inputs['crop_directory'])}
        if operation == 'retrieve':
            from .retrieval import hybrid_search
            return {'data': hybrid_search(**inputs)}
        return self.vlm(operation, inputs)

    def load(self, kind):
        if kind in self.loaded:
            return self.loaded[kind]
        import torch
        from transformers import (AutoModel, AutoProcessor, AutoTokenizer, CLIPImageProcessor,
                                  AutoModelForImageTextToText, AutoModelForZeroShotObjectDetection)
        cfg = self.cfg['models']
        device = cfg['device']
        if device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA unavailable; use a GPU allocation or explicitly set models.device=cpu')
        if device == 'cuda' and self.gpu_acquired_at is None:
            import time
            torch.cuda.init()
            self.gpu_acquired_at = time.monotonic()
        torch.manual_seed(self.cfg['seed'])
        name = cfg[kind]
        options = {'local_files_only': cfg['local_files_only']}
        if cfg['revisions'].get(kind):
            options['revision'] = cfg['revisions'][kind]
        dtype = torch.bfloat16 if device == 'cuda' and cfg['precision'] == 'bfloat16' else torch.float32
        if kind == 'visual_encoder':
            model = AutoModel.from_pretrained(name, trust_remote_code=True, torch_dtype=dtype, **options).to(device).eval()
            # EVA's released model has no processor file; its visual config specifies 224.
            size = model.config.vision_config.image_size
            processor = CLIPImageProcessor(size={'shortest_edge': size}, crop_size={'height': size, 'width': size})
        elif kind == 'vlm':
            processor = AutoProcessor.from_pretrained(name, **options)
            model = AutoModelForImageTextToText.from_pretrained(name, torch_dtype=dtype,
                                                               device_map=device, attn_implementation='sdpa', **options).eval()
        elif kind == 'detector':
            processor = AutoProcessor.from_pretrained(name, **options)
            model = AutoModelForZeroShotObjectDetection.from_pretrained(name, **options).to(device).eval()
        else:
            processor = AutoTokenizer.from_pretrained(name, **options)
            model = AutoModel.from_pretrained(name, torch_dtype=dtype, **options).to(device).eval()
        self.loaded[kind] = model, processor
        return model, processor

    def features(self, frames):
        import torch
        from PIL import Image
        model, processor = self.load('visual_encoder')
        result = []
        batch_size = self.cfg['models']['batching']['visual_encoder']
        for start in range(0, len(frames), batch_size):
            self.check_deadline()
            images = []
            for f in frames[start:start+batch_size]:
                with Image.open(f['path']) as image:
                    images.append(image.convert('RGB'))
            inputs = processor(images=images, return_tensors='pt', input_data_format='channels_last').pixel_values.to(model.device, dtype=model.dtype)
            self.model_pixels['visual_encoder'] = self.model_pixels.get('visual_encoder', 0)+inputs.numel()//3
            with torch.inference_mode():
                features = model.encode_image(inputs).float()
                features = torch.nn.functional.normalize(features, dim=-1)
            result.extend(features.cpu().tolist())
        return result

    def embed(self, texts):
        import torch
        model, tokenizer = self.load('text_embedding')
        out = []
        if not texts:
            return out
        lengths = [len(ids) for ids in tokenizer(texts, padding=False, truncation=False)['input_ids']]
        if any(length > 8192 for length in lengths):
            raise ValueError('BGE-M3 input exceeds 8192 tokens; split explicitly')
        cfg = self.cfg['models']['batching']
        start = 0
        while start < len(texts):
            self.check_deadline()
            end, longest = start+1, lengths[start]
            while end < len(texts) and end-start < cfg['text_embedding']:
                next_longest = max(longest, lengths[end])
                if next_longest*(end-start+1) > cfg['text_embedding_max_tokens']:
                    break
                longest, end = next_longest, end+1
            inputs = tokenizer(texts[start:end], padding=True, return_tensors='pt', truncation=False).to(model.device)
            self.model_tokens['text_embedding'] = self.model_tokens.get('text_embedding', 0)+int(inputs.attention_mask.sum())
            with torch.inference_mode():
                vector = model(**inputs).last_hidden_state[:, 0].float()
                vector = torch.nn.functional.normalize(vector, dim=-1)
            out.extend(vector.cpu().tolist())
            start = end
        return out

    def detect(self, frames, terms, crop_directory):
        import torch
        from PIL import Image
        from .config import digest
        from .state import bbox_valid
        model, processor = self.load('detector')
        cfg = self.cfg['materialization']
        directory = Path(crop_directory)
        directory.mkdir(parents=True, exist_ok=True)
        detections = []
        for f in frames:
            self.check_deadline()
            with Image.open(f['path']) as source:
                image = source.convert('RGB')
            text = '. '.join(t.lower().strip('. ') for t in terms)+'.'
            inputs = processor(images=image, text=text, return_tensors='pt').to(model.device)
            self.model_pixels['detector'] = self.model_pixels.get('detector', 0)+inputs.pixel_values.numel()//3
            self.model_tokens['detector'] = self.model_tokens.get('detector', 0)+int(inputs.attention_mask.sum())
            with torch.inference_mode():
                output = model(**inputs)
            results = processor.post_process_grounded_object_detection(
                output, inputs.input_ids, threshold=cfg['detection_box_threshold'],
                text_threshold=cfg['detection_text_threshold'], target_sizes=[(image.height, image.width)])[0]
            labels = results.get('text_labels')
            if labels is None:
                labels = results.get('labels', [])
            for box, score, label in zip(results['boxes'], results['scores'], labels):
                coords = box.cpu().tolist()
                normalized = [max(0., min(1., p/s)) for p, s in zip(coords, [image.width, image.height]*2)]
                if not bbox_valid(normalized):
                    continue
                crop = image.crop(tuple(round(p*s) for p, s in zip(normalized, [image.width, image.height]*2)))
                if min(crop.size) == 0:
                    continue
                path = directory / (digest([f['frame_id'], normalized])+'.png')
                crop.save(path)
                if f.get('crop'):
                    region = f['crop']
                    normalized = [region[0]+normalized[0]*(region[2]-region[0]),
                                  region[1]+normalized[1]*(region[3]-region[1]),
                                  region[0]+normalized[2]*(region[2]-region[0]),
                                  region[1]+normalized[3]*(region[3]-region[1])]
                detections.append({'frame_id': f['frame_id'], 'pts': f['pts'], 'bbox': normalized,
                                   'category': str(label).lower(), 'aliases': [], 'score': float(score),
                                   'crop_ref': str(path), 'width': crop.width, 'height': crop.height})
        crop_frames = [{'path': d['crop_ref']} for d in detections]
        features = self.features(crop_frames) if detections else []
        for d, vector in zip(detections, features):
            d['feature'] = vector
        return detections

    def vlm(self, operation, inputs):
        import torch
        from PIL import Image, ImageDraw
        from .context import operation_inputs
        inputs = operation_inputs(operation, inputs)
        model, processor = self.load('vlm')
        # Short per-call aliases keep stable storage IDs out of the output token budget.
        # Only exact IDs in this authorized input can be expanded back into stored references.
        identifiers = []
        def collect(value):
            if isinstance(value, dict):
                for field in ('id', 'frame_id'):
                    if isinstance(value.get(field), str) and value[field] not in identifiers:
                        identifiers.append(value[field])
                for v in value.values():
                    collect(v)
            elif isinstance(value, list):
                for v in value:
                    collect(v)
        collect(inputs)
        aliases = {ref: f'ID{i}' for i, ref in enumerate(identifiers)}
        inverse = {v: k for k, v in aliases.items()}
        def substitute(value, mapping):
            if isinstance(value, dict):
                return {mapping.get(k, k): substitute(v, mapping) for k, v in value.items()}
            if isinstance(value, list):
                return [substitute(v, mapping) for v in value]
            return mapping.get(value, value) if isinstance(value, str) else value
        content, images = [], []
        for f in inputs.get('frames', []):
            with Image.open(f['path']) as source:
                image = source.convert('RGB')
            boxes = [b for o in inputs.get('occurrences', []) for b in o.get('boxes', [])
                     if b['frame_id'] == f['frame_id']]
            if boxes:
                draw = ImageDraw.Draw(image)
                for o in inputs.get('occurrences', []):
                    for b in o.get('boxes', []):
                        if b['frame_id'] != f['frame_id']:
                            continue
                        box = b['bbox']
                        if f.get('crop'):
                            region = f['crop']
                            box = [(box[0]-region[0])/(region[2]-region[0]), (box[1]-region[1])/(region[3]-region[1]),
                                   (box[2]-region[0])/(region[2]-region[0]), (box[3]-region[1])/(region[3]-region[1])]
                        coordinates = tuple(x*s for x, s in zip(box, [image.width, image.height]*2))
                        draw.rectangle(coordinates, outline='red', width=2)
                        draw.text(coordinates[:2], aliases[o['id']], fill='red')
            content.extend([{'type': 'text', 'text': f"frame_id={aliases[f['frame_id']]} PTS={f['pts']}"},
                            {'type': 'image'}])
            images.append(image)
        from .context import model_view, encode_with_frame_budget, encode_caption_context
        from .schemas import output_schema, decoder_schema
        schema = decoder_schema(substitute(output_schema(operation, inputs), aliases))
        contract = CONTRACTS[operation]
        if operation == 'answer' and inputs.get('answer_strategy') == 'direct':
            contract = CONTRACTS['answer_direct']
        if operation == 'repair':
            original = inputs['original_operation']
            if original == 'answer' and inputs.get('original_inputs', {}).get('answer_strategy') == 'direct':
                original = 'answer_direct'
            contract += '\n原模块输出契约：\n'+CONTRACTS[original]
        def render(model_inputs):
            payload = substitute(model_view(model_inputs), aliases)
            if 'caption_timeline' in payload:
                # The full option texts are already beside their IDs. Repeating
                # their differing clauses can duplicate almost every long option.
                payload.pop('option_distinctions', None)
                # Flatten after aliasing so printed caption IDs match the decoder
                # allowlist. Keep each original second next to its exact text.
                payload['caption_timeline'] = [
                    f"[{c['id']} {c['interval'][0]:g}-{c['interval'][1]:g}s] {c['text']}"
                    for c in payload['caption_timeline']]
            text_part = {'type': 'text', 'text': '<input>\n'+json.dumps(payload, ensure_ascii=False)+
                         '\n</input>\n实体框及ID标在所有对应图像上；存储元数据省略，事实文本完整保留。'
                         '\n仅返回任务要求的JSON字段，不复制输入对象。'}
            messages = [{'role': 'system', 'content': SYSTEM+'\n'+contract},
                        {'role': 'user', 'content': content+[text_part]}]
            return processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        caption_selection = None
        if operation in ('answer', 'gate') and not images and 'caption_timeline' not in inputs:
            encoded, caption_selection = encode_caption_context(processor, render, inputs, self.cfg['models']['max_input_tokens'])
            frame_views = []
        else:
            encoded, frame_views = encode_with_frame_budget(processor, render(inputs), images, self.cfg['models']['max_input_tokens'])
        encoded = encoded.to(model.device)
        input_tokens = encoded.input_ids.shape[-1]
        self.model_tokens['vlm'] = self.model_tokens.get('vlm', 0)+input_tokens
        if 'pixel_values' in encoded:
            self.model_pixels['vlm'] = self.model_pixels.get('vlm', 0)+encoded.pixel_values.numel()//3
        if input_tokens > self.cfg['models']['max_input_tokens']:
            raise ValueError(f'Context overflow ({input_tokens}); explicit batching required, no truncation allowed')
        from lmformatenforcer import JsonSchemaParser
        from lmformatenforcer.integrations.transformers import build_transformers_prefix_allowed_tokens_fn
        self.prepare_vlm()
        prefix = build_transformers_prefix_allowed_tokens_fn(self.enforcer_data, JsonSchemaParser(schema))
        with torch.inference_mode():
            self.check_deadline()
            kwargs = dict(self.cfg['models']['generation'], prefix_allowed_tokens_fn=prefix)
            if self.deadline is not None:
                kwargs['stopping_criteria'] = deadline_stopping_criteria(self.deadline)
            generated = model.generate(**encoded, **kwargs)
        self.check_deadline()
        tokens = generated[0, input_tokens:]
        output = processor.tokenizer.decode(tokens, skip_special_tokens=True).strip()
        try:
            clean = output
            if clean.startswith('```'):
                clean = clean.split('\n', 1)[1].rsplit('```', 1)[0]
            old_map = inputs.get('original_output', {}).get('id_map', {}) if operation == 'repair' else {}
            data = substitute(json.loads(clean), old_map | inverse)
        except (ValueError, IndexError):
            data = output
        return {'data': data, 'id_map': inverse,
                'usage': {'input_tokens': input_tokens, 'output_tokens': len(tokens),
                          'model_caption_context': caption_selection,
                          'model_frame_views': [dict(frame_id=f['frame_id'], **view)
                                                for f, view in zip(inputs.get('frames', []), frame_views)],
                          'context_projection': 'facts_and_all_frames_v1'}}

    def prepare_vlm(self):
        import time
        if self.enforcer_data is not None and 'vlm' in self.loaded:
            return {'model_load_seconds': 0., 'tokenizer_constraint_seconds': 0.}
        started = time.monotonic()
        print('MODEL_PREPARE loading model and processor', flush=True)
        _, processor = self.load('vlm')
        loaded = time.monotonic()
        print(f'MODEL_PREPARE model ready after {loaded-started:.2f}s; preparing tokenizer constraints', flush=True)
        if self.enforcer_data is None:
            # The upstream builder repeatedly resolves all_special_ids for every vocabulary token.
            # Batch decoding is equivalent and keeps the one-time tokenizer preparation bounded.
            from functools import partial
            from lmformatenforcer import TokenEnforcerTokenizerData
            from lmformatenforcer.integrations.transformers import _decode_function
            tokenizer = processor.tokenizer
            special = set(tokenizer.all_special_ids)
            zero = tokenizer.encode('0')[-1]
            regular = []
            vocab_size = len(tokenizer)
            for start in range(0, vocab_size, 4096):
                ids = [i for i in range(start, min(start+4096, vocab_size)) if i not in special]
                after_zero = tokenizer.batch_decode([[zero, i] for i in ids])
                alone = tokenizer.batch_decode([[i] for i in ids])
                regular.extend((i, a[1:], len(a[1:]) > len(b)) for i, a, b in zip(ids, after_zero, alone))
            self.enforcer_data = TokenEnforcerTokenizerData(regular, partial(_decode_function, tokenizer),
                                                           tokenizer.eos_token_id, False, vocab_size)
        ended = time.monotonic()
        print(f'MODEL_PREPARE ready; tokenizer constraints took {ended-loaded:.2f}s', flush=True)
        return {'model_load_seconds': loaded-started, 'tokenizer_constraint_seconds': ended-loaded}


def preflight(cfg):
    """Resolve actual snapshots and hash configuration, prompts, software and weight files."""
    import importlib.metadata
    from huggingface_hub import snapshot_download
    from .config import digest
    from .video import file_hash
    frozen = json.loads(json.dumps(cfg))
    artifacts = {}
    for kind in ('visual_encoder', 'detector', 'vlm', 'text_embedding'):
        name = cfg['models'][kind]
        path = Path(name)
        if not path.is_dir():
            path = Path(snapshot_download(name, revision=cfg['models']['revisions'].get(kind),
                                          local_files_only=cfg['models']['local_files_only']))
        files = sorted(p for p in path.rglob('*') if p.is_file() and p.suffix in ('.json', '.bin', '.safetensors', '.py', '.txt'))
        if not any(p.suffix in ('.bin', '.safetensors') for p in files):
            raise ValueError(f'Missing weights for {kind}: {name}')
        hashes = {str(p.relative_to(path)): file_hash(p) for p in files}
        artifacts[kind] = {'path': str(path.resolve()), 'hash': digest(hashes), 'files': hashes}
        frozen['models'][kind] = str(path.resolve())
        frozen['models']['revisions'][kind] = artifacts[kind]['hash']
    artifacts['software'] = {name: importlib.metadata.version(name) for name in
                             ('torch', 'transformers', 'av', 'numpy', 'scipy', 'jieba', 'PyYAML', 'lm-format-enforcer')}
    artifacts['software_lock'] = {d.metadata['Name']: d.version for d in importlib.metadata.distributions() if d.metadata.get('Name')}
    artifacts['software_lock_hash'] = digest(artifacts['software_lock'])
    from .prompts import prompt_hash
    artifacts['prompts'] = {name: prompt_hash(name) for name in CONTRACTS}
    source_root = Path(__file__).resolve().parent
    artifacts['source_hashes'] = {p.name: file_hash(p) for p in sorted(source_root.glob('*.py'))}
    frozen['models']['revisions']['@online'] = digest([artifacts['source_hashes'], artifacts['software_lock_hash'], artifacts['prompts']])
    import platform
    artifacts['hardware'] = {'platform': platform.platform(), 'processor': platform.processor()}
    return frozen, artifacts
