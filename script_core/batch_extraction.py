"""Independent F extraction microbatches for production and diagnostic use.

Each row has one original prompt and one source-specific decoder. Batching only
changes execution: it never concatenates source statements or unions allowlists.
"""
from copy import deepcopy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_runtime(directory):
    directory = Path(directory).resolve()
    name = '_batch_runtime_' + hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, directory/'__init__.py',
            submodule_search_locations=[str(directory)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
    return SimpleNamespace(**{part: importlib.import_module(name+'.'+module) for part, module in
        dict(backend='backend', memory='lazy_script_memory', context='context',
             schemas='schemas', prompts='prompts', config='config').items()})


def substitute(value, mapping):
    if isinstance(value, dict):
        return {mapping.get(k, k): substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, mapping) for v in value]
    return mapping.get(value, value) if isinstance(value, str) else value


def render_request(inputs, runtime, processor):
    if set(inputs) != {'unit', 'requested_fields', 'source_type'} or inputs['source_type'] != 'caption':
        raise ValueError('Batch extraction accepts one source-only F request, without task or images')
    if set(inputs['unit']) != {'id', 'text'} or not inputs['unit']['text'].strip():
        raise ValueError('Expected one nonempty source with its local ID')
    aliases = {inputs['unit']['id']: 'ID0'}
    schema = runtime.memory.output_schema(runtime.memory.EXTRACT, inputs)
    decoder = runtime.schemas.decoder_schema(substitute(schema, aliases))
    # Exact text-only rendering used by the copied F LocalModels.vlm.
    instruction = '输入仅为带来源和时间戳的文本描述，不含图像。不得声称已看图或得到视觉核验。'
    text = ('<input>\n'+json.dumps(substitute(runtime.context.model_view(inputs), aliases), ensure_ascii=False)
        +'\n</input>\n'+instruction+'\n仅返回任务要求的JSON字段，不复制输入对象。')
    messages = [dict(role='system', content=runtime.prompts.SYSTEM+'\n'+runtime.memory.EXTRACT_PROMPT),
                dict(role='user', content=[dict(type='text', text=text)])]
    prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    return dict(prompt=prompt, schema=schema, decoder_schema=decoder,
                inverse={v: k for k, v in aliases.items()}, messages=messages)


class IndependentPrefixes:
    """Route each generation row to its own LMFE parser, including after EOS."""
    def __init__(self, prefixes, prompt_width, eos_ids, pad_id):
        self.prefixes = prefixes
        self.prompt_width = prompt_width
        self.eos_ids = set(eos_ids)
        self.pad_id = pad_id

    def __call__(self, batch_id, tokens):
        if not 0 <= batch_id < len(self.prefixes):
            raise ValueError('Unexpected beam/return sequence; only one greedy sequence per source is supported')
        suffix = tokens[self.prompt_width:].tolist()
        if self.eos_ids.intersection(suffix):
            return [self.pad_id]
        return self.prefixes[batch_id](0, tokens)


def trim_generated(ids, eos_ids):
    for index, value in enumerate(ids):
        if value in eos_ids:
            return ids[:index+1]
    return ids


class BatchExtractor:
    def __init__(self, cfg, runtime, models=None):
        self.cfg, self.runtime = cfg, runtime
        generation = cfg['models']['generation']
        if generation.get('do_sample', False) or generation.get('num_beams', 1) != 1 or generation.get('num_return_sequences', 1) != 1:
            raise ValueError('The benchmark requires the original single-sequence greedy generation')
        self.models = models if models is not None else runtime.backend.LocalModels(cfg)

    def serial(self, inputs):
        return self.models.call(self.runtime.memory.EXTRACT, deepcopy(inputs))

    def generate(self, requests):
        if not requests or len(requests) > 4:
            raise ValueError('A microbatch must contain one to four independent sources')
        import torch
        model, processor = self.models.load('vlm')
        self.models.prepare_vlm()
        packets = [render_request(request, self.runtime, processor) for request in requests]
        tokenizer = processor.tokenizer
        old_padding = tokenizer.padding_side
        try:
            tokenizer.padding_side = 'left'
            encoded = processor(text=[p['prompt'] for p in packets], images=None,
                padding=True, return_tensors='pt')
        finally:
            tokenizer.padding_side = old_padding
        width = encoded.input_ids.shape[-1]
        from .generation_capacity import context_preflight
        capacity = context_preflight(getattr(model, 'config', None), int(width), 1024,
            self.cfg['models']['max_input_tokens'], label='Extraction batch')
        input_tokens = encoded.attention_mask.sum(dim=1).tolist()
        eos = getattr(model.generation_config, 'eos_token_id', None)
        eos_ids = set(eos if isinstance(eos, list) else [eos] if eos is not None else [])
        if tokenizer.eos_token_id is not None:
            eos_ids.add(tokenizer.eos_token_id)
        pad_id = tokenizer.pad_token_id
        if pad_id is None or not eos_ids:
            raise ValueError('Explicit padding and EOS tokens are required for independent batch completion')
        prefixes = [self.runtime.backend.constrained_prefix(self.models.enforcer_data,
            p['decoder_schema'], self.runtime.memory.EXTRACT) for p in packets]
        route = IndependentPrefixes(prefixes, width, eos_ids, pad_id)
        kwargs = dict(self.cfg['models']['generation'], prefix_allowed_tokens_fn=route,
            max_new_tokens=1024, pad_token_id=pad_id)
        if self.models.deadline is not None:
            kwargs['stopping_criteria'] = self.runtime.backend.deadline_stopping_criteria(self.models.deadline)
        self.models.check_deadline()
        with torch.inference_mode():
            generated = model.generate(**encoded.to(model.device), **kwargs)
        self.models.check_deadline()
        outputs = []
        for index, packet in enumerate(packets):
            tokens = trim_generated(generated[index, width:].tolist(), eos_ids)
            raw = tokenizer.decode(tokens, skip_special_tokens=True).strip()
            try:
                data = substitute(self.runtime.backend.decode_structured_text(raw), packet['inverse'])
            except (ValueError, IndexError):
                data = raw
            outputs.append(dict(data=data, raw_text=raw, id_map=packet['inverse'],
                capacity_preflight=capacity,
                usage=dict(input_tokens=int(input_tokens[index]), output_tokens=len(tokens))))
        return outputs


def inspect_output(inputs, output, runtime):
    """Use the frozen F schema and literal binding checks, keeping failures explicit."""
    try:
        data = deepcopy(output['data'])
        if isinstance(data, str):
            data = runtime.backend.decode_structured_text(data)
        schema = runtime.memory.output_schema(runtime.memory.EXTRACT, inputs)
        data = runtime.schemas.restore_empty_arrays(data, schema)
        runtime.schemas.validate_output(data, schema)
        accept = getattr(runtime.memory, 'inspect_accept', runtime.memory.joint.accept)
        accepted = accept(inputs['unit'], data)
        whole = runtime.memory.joint.complete_simple_source(inputs['unit'], accepted)
        return dict(data=data, accepted=accepted, complete_simple=whole, error=None,
            retained_original=None if whole else inputs['unit']['text'])
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        return dict(data=None, accepted=None, complete_simple=False,
            error=f'{type(exc).__name__}: {exc}', retained_original=inputs['unit']['text'])


def eligible_gpu(row, protected_uuids, allowed_indices, minimum_free_mib=32768):
    return (row['uuid'] not in protected_uuids and row['index'] in allowed_indices
        and not row['compute_pids'] and row['utilization'] <= 5
        and row['used_mib'] <= 500 and row['free_mib'] >= minimum_free_mib)


def benchmark_report(requests, calls, variants):
    """Charge each batch once. Compare outputs with the matched serial control."""
    serial = {item['request_id']: item for call in calls if call['variant'] == 'serial'
              for item in call.get('items', [])}
    expected = {r['id']: r for r in requests}
    result = {}
    for variant in variants:
        batches = [c for c in calls if c['variant'] == variant]
        items = [item for c in batches for item in c.get('items', [])]
        ids = [item['request_id'] for item in items]
        if len(ids) != len(set(ids)) or not set(ids) <= expected.keys():
            raise ValueError('Duplicate or foreign benchmark response')
        seconds = sum(c['seconds'] for c in batches)
        matched = [item for item in items if item['request_id'] in serial]
        result[variant] = dict(completed=len(items), planned=len(requests),
            batch_count=len(batches), failed_batches=sum(bool(c.get('error')) for c in batches),
            seconds=seconds, mean_seconds_per_source=seconds/len(items) if items else None,
            schema_errors=sum(bool(i['inspection']['error']) for i in items),
            rejected_facts=sum(len((i['inspection']['accepted'] or {}).get('rejected', [])) for i in items),
            retained_original_sources=sum(i['inspection']['retained_original'] is not None for i in items),
            matched_serial_sources=len(matched),
            exact_serial_matches=sum(not i['inspection']['error']
                and not serial[i['request_id']]['inspection']['error']
                and i['inspection']['data'] == serial[i['request_id']]['inspection']['data'] for i in matched),
            accepted_serial_matches=sum(i['inspection']['accepted'] is not None
                and i['inspection']['accepted'] == serial[i['request_id']]['inspection']['accepted'] for i in matched),
            matching_input_tokens=sum(i.get('output', {}).get('usage', {}).get('input_tokens')
                == serial[i['request_id']].get('output', {}).get('usage', {}).get('input_tokens') for i in matched),
            peak_allocated_bytes=max((c.get('peak_allocated_bytes', 0) for c in batches), default=0))
    baseline = result.get('serial', {})
    for variant, value in result.items():
        value['equivalent_to_serial'] = (value['completed'] == len(requests)
            and value['matched_serial_sources'] == len(requests) and value['schema_errors'] == 0
            and value['accepted_serial_matches'] == len(requests)
            and value['matching_input_tokens'] == len(requests))
        value['speedup_vs_serial'] = (baseline['seconds']/value['seconds']
            if variant != 'serial' and value['seconds'] and value['completed'] == baseline.get('completed')
            and value['completed'] == len(requests) and not value['failed_batches']
            and not value['schema_errors'] and not baseline.get('schema_errors') else None)
    return dict(variants=result, complete=all(v['completed'] == len(requests) and not v['failed_batches']
        for v in result.values()), scope='Source-only extraction benchmark; no new QA accuracy measured')
