"""Independent-source G microbatches; frozen prompts, schemas and review policy.

This is an execution experiment. It does not patch a running G worker, shorten
prompts, concatenate sources, or replace the original final-QA path.
"""
from copy import deepcopy
import importlib
import json

from .batch_extraction import IndependentPrefixes, substitute, trim_generated


VERSION = 'G-independent-source-batch-v1'


def bind_runtime(runtime):
    runtime.generalized = importlib.import_module(runtime.backend.__package__ + '.generalized_entity_memory')
    return runtime


def source_inputs(unit, core, review=None):
    value = dict(unit=dict(id=unit['id'], text=core.clean_unit(unit)['text']), source_type='caption')
    if review is not None:
        value['review'] = deepcopy(review)
    return value


def render_request(inputs, runtime, processor):
    if set(inputs) not in ({'unit', 'source_type'}, {'unit', 'source_type', 'review'}):
        raise ValueError('Only one G source and its optional same-source review are allowed')
    if inputs['source_type'] != 'caption' or set(inputs['unit']) != {'id', 'text'} or not inputs['unit']['text'].strip():
        raise ValueError('Expected a nonempty source-only caption request')
    aliases = {inputs['unit']['id']: 'ID0'}
    schema = runtime.generalized.output_schema(inputs)
    instruction = '输入仅为带来源和时间戳的文本描述，不含图像。不得声称已看图或得到视觉核验。'
    text = ('<input>\n' + json.dumps(substitute(runtime.context.model_view(inputs), aliases), ensure_ascii=False)
        + '\n</input>\n' + instruction + '\n仅返回任务要求的JSON字段，不复制输入对象。')
    messages = [dict(role='system', content=runtime.prompts.SYSTEM + '\n' + runtime.generalized.PROMPT),
                dict(role='user', content=[dict(type='text', text=text)])]
    return dict(prompt=processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True),
        messages=messages, schema=schema, decoder_schema=runtime.schemas.decoder_schema(substitute(schema, aliases)),
        inverse={v: k for k, v in aliases.items()})


class GeneralizedExtractor:
    def __init__(self, cfg, runtime, models=None):
        self.cfg, self.runtime = cfg, runtime
        generation = cfg['models']['generation']
        if (generation.get('do_sample', False) or generation.get('num_beams', 1) != 1
                or generation.get('num_return_sequences', 1) != 1):
            raise ValueError('Only the frozen single-sequence greedy generation is supported')
        if cfg.get('query', {}).get('json_repair_retries', 0) != 0:
            raise ValueError('This executor requires the frozen zero JSON-repair policy')
        self.models = models if models is not None else runtime.backend.LocalModels(cfg)

    def serial(self, inputs):
        return self.models.call(self.runtime.generalized.OPERATION, deepcopy(inputs))

    def generate(self, requests):
        if not 1 <= len(requests) <= 4:
            raise ValueError('Use one to four independent G sources')
        import torch
        model, processor = self.models.load('vlm')
        self.models.prepare_vlm()
        packets = [render_request(r, self.runtime, processor) for r in requests]
        tokenizer = processor.tokenizer
        previous = tokenizer.padding_side
        try:
            tokenizer.padding_side = 'left'
            encoded = processor(text=[p['prompt'] for p in packets], images=None, padding=True, return_tensors='pt')
        finally:
            tokenizer.padding_side = previous
        width = encoded.input_ids.shape[-1]
        if width > self.cfg['models']['max_input_tokens']:
            raise ValueError('Context overflow; no source truncation is allowed')
        input_tokens = encoded.attention_mask.sum(dim=1).tolist()
        eos = model.generation_config.eos_token_id
        eos_ids = set(eos if isinstance(eos, list) else [eos] if eos is not None else [])
        if tokenizer.eos_token_id is not None:
            eos_ids.add(tokenizer.eos_token_id)
        pad_id = tokenizer.pad_token_id
        if pad_id is None or not eos_ids:
            raise ValueError('Explicit pad/EOS tokens are required')
        prefixes = [self.runtime.backend.constrained_prefix(self.models.enforcer_data,
            p['decoder_schema'], self.runtime.generalized.OPERATION) for p in packets]
        route = IndependentPrefixes(prefixes, width, eos_ids, pad_id)
        kwargs = dict(self.cfg['models']['generation'], prefix_allowed_tokens_fn=route,
            max_new_tokens=1536, pad_token_id=pad_id)
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
                usage=dict(input_tokens=int(input_tokens[index]), output_tokens=len(tokens))))
        return outputs


def inspect_output(unit, inputs, output, runtime):
    data = deepcopy(output['data'])
    if isinstance(data, str):
        data = runtime.backend.decode_structured_text(data)
    if isinstance(data, dict) and set(data) == {'data'} and isinstance(data['data'], dict):
        data = data['data']
    schema = runtime.generalized.output_schema(inputs)
    data = runtime.schemas.restore_empty_arrays(data, schema)
    runtime.schemas.validate_output(data, schema)
    return dict(data=data, accepted=runtime.generalized.accept(unit, data))


def guarded_result():
    return dict(accepted=dict(facts=[], mentions=[], rejected=[], uncertain=True),
        complete_simple=False, role_review=False, error=None, guarded_original=True)


def extract_sources(requests, size, runtime, invoke, progress=None):
    """Same G guard, acceptance and at-most-one full role review per source.

    invoke(phase, jobs) receives independent (request_id, inputs) pairs. It owns
    timing, persistence and failure handling; a failed batch is never retried or
    silently split here. Invalid source JSON retains that source's original.
    """
    if size not in (1, 2, 4):
        raise ValueError('Supported execution sizes are 1, 2, 4')
    ids = [r['id'] for r in requests]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate request ID')
    core = runtime.generalized
    results, inspections = {}, {}

    def receive(request, inputs, output, reviewed):
        try:
            inspected = inspect_output(request['unit'], inputs, output, runtime)
            accepted = inspected['accepted']
            inspections[request['id']] = inspected
            results[request['id']] = dict(accepted=accepted,
                complete_simple=core.complete_simple_source(request['unit'], accepted),
                role_review=reviewed, error=None)
            return [r for r in accepted['rejected'] if r['record']['kind'] == 'action']
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            results[request['id']] = dict(accepted=dict(facts=[], mentions=[], rejected=[], uncertain=True),
                complete_simple=False, role_review=False,
                error=dict(type='ToolFailure', message=f'Invalid {core.OPERATION} JSON: {exc}'))
            return []

    for offset in range(0, len(requests), size):
        group = requests[offset:offset + size]
        pending = []
        for request in group:
            inputs = source_inputs(request['unit'], core)
            if core.NEGATION.search(inputs['unit']['text']) or core.UNCERTAINTY.search(inputs['unit']['text']):
                results[request['id']] = guarded_result()
            else:
                pending.append((request, inputs))
        reviews = []
        if pending:
            outputs = invoke('initial', [(r['id'], inp) for r, inp in pending])
            if len(outputs) != len(pending):
                raise ValueError('Missing or extra source responses')
            for (request, inputs), output in zip(pending, outputs):
                bad = receive(request, inputs, output, False)
                if bad:
                    reviews.append((request, source_inputs(request['unit'], core, review=bad)))
        if reviews:
            outputs = invoke('review', [(r['id'], inp) for r, inp in reviews])
            if len(outputs) != len(reviews):
                raise ValueError('Missing or extra review responses')
            for (request, inputs), output in zip(reviews, outputs):
                receive(request, inputs, output, True)
        if progress:
            progress(min(offset + size, len(requests)))
    return {uid: results[uid] for uid in ids}
