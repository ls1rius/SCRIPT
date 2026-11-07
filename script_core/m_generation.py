"""Shared L/M/N live adapter to the constrained evidence planner and reader."""
import torch


def generate(backend, operation, request):
    label = {'oplus_answer': 'O+', 'qplus_answer': 'Q+'}.get(operation) or {'l': 'L', 'm': 'M', 'n': 'N', 'o': 'O', 'p': 'P', 'q': 'Q'}[operation[0]]
    from lmformatenforcer.integrations.transformers import build_transformers_prefix_allowed_tokens_fn
    from script.run_evidence_planner_experiment import (
        nonempty_plan_parser, strict_answer_parser, parse_strict_answer,
        PLANNER_MAX_NEW_TOKENS, PLANNER_DECODER, READER_PROTOCOL)
    from .backend import deadline_stopping_criteria, SoftDeadlineExceeded
    from .videotree_tools import OriginalModel
    if not hasattr(backend, '_m_model'):
        backend._m_model = OriginalModel(backend)
    model = backend._m_model.qa
    backend.prepare_vlm()
    if not hasattr(backend, '_m_tokenizer_data'):
        _, processor = backend.load('vlm')
        # Reuse the worker's batched tokenizer preparation after checking that
        # the original QA tokenizer has the identical token alphabet/template.
        if (model.tokenizer.get_vocab() != processor.tokenizer.get_vocab()
                or model.tokenizer.chat_template != processor.tokenizer.chat_template
                or model.tokenizer.all_special_ids != processor.tokenizer.all_special_ids):
            raise ValueError(f'{label} reader and worker tokenizers differ')
        backend._m_tokenizer_data = backend.enforcer_data
    inputs = request['inputs']
    messages = [dict(role='system', content=inputs['head']),
                dict(role='user', content=inputs['prompts'][0])]
    if len(inputs['prompts']) != 1:
        raise ValueError(f'{label} requires one complete, untruncated prompt')
    encoded = model.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
        return_dict=True, return_tensors='pt').to(model.model.device)
    n = int(encoded.input_ids.shape[1])
    planning = operation in ('l_evidence_plan', 'm_evidence_plan', 'n_evidence_plan')
    output_limit = PLANNER_MAX_NEW_TOKENS if planning else 1024
    from .generation_capacity import context_preflight, planner_output_preflight
    capacity = context_preflight(getattr(model.model, 'config', None), n, output_limit,
                                 backend.cfg['models']['max_input_tokens'], label=label)
    if planning:
        capacity['planner_output'] = planner_output_preflight(
            request['schema'], model.tokenizer, backend._m_tokenizer_data, output_limit)
    parser = nonempty_plan_parser(request['schema']) if planning else strict_answer_parser(request['options'])
    prefix = build_transformers_prefix_allowed_tokens_fn(backend._m_tokenizer_data, parser)
    kwargs = dict(do_sample=False, max_new_tokens=PLANNER_MAX_NEW_TOKENS if planning else 1024,
                  prefix_allowed_tokens_fn=prefix)
    if backend.deadline is not None:
        kwargs['stopping_criteria'] = deadline_stopping_criteria(backend.deadline)
    backend.check_deadline()
    with torch.inference_mode():
        generated = model.model.generate(**encoded, **kwargs)
    tokens = generated[0, n:]
    raw = model.tokenizer.decode(tokens, skip_special_tokens=True).strip()
    eos = model.model.generation_config.eos_token_id
    eos = [eos] if isinstance(eos, int) else eos or []
    backend.model_tokens['vlm'] = backend.model_tokens.get('vlm', 0) + n
    ended = bool(len(tokens)) and int(tokens[-1]) in eos
    # Retain an interrupted prefix before cooperative deadline enforcement.
    # This is diagnostic output only; deadlines and incomplete EOS still fail.
    diagnostic = dict(raw=raw, generated_token_ids=tokens.tolist(),
        ended_normally=ended, max_new_tokens=output_limit, capacity_preflight=capacity,
        usage=dict(input_tokens=n, output_tokens=len(tokens)))
    try:
        backend.check_deadline()
    except SoftDeadlineExceeded as exc:
        exc.raw_output = diagnostic
        raise
    if not ended:
        exc = ValueError(f'{label} generation did not terminate normally; no automatic retry')
        exc.raw_output = diagnostic
        raise exc
    result = dict(raw=raw, decoder=PLANNER_DECODER if planning else READER_PROTOCOL,
                  capacity_preflight=capacity,
                  usage=dict(input_tokens=n, output_tokens=len(tokens)))
    if not planning:
        parsed = parse_strict_answer(raw, request['options'])
        if parsed['option'] is None:
            raise ValueError(f'Invalid {label} constrained answer')
        result['data'] = dict(selected_option=str(parsed['option']), raw=raw)
    return result
