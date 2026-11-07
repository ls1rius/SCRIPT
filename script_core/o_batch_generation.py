"""Independent O prompts in a tensor batch; never concatenate evidence rows."""
import json
import torch
from . import o_sentence_filter as core
from .o_generation import decoder_contract, constrained_prefix
from .schemas import restore_empty_arrays
from .config import digest

OPERATION = 'o_independent_batch'
PROMPTS = {core.EXTRACT: core.EXTRACT_PROMPT, core.FILTER: core.FILTER_PROMPT,
           core.COMPOSE: core.COMPOSE_PROMPT}


def validate_packet(packet):
    from .q_repair import OPS as Q3_OPS
    from .q_v4 import NEW_OPS as Q4_OPS
    rows = packet['items']; scope = packet['scope']; operation = packet['operation']
    if operation not in (*PROMPTS, *Q3_OPS, *Q4_OPS, 'p_filter', 'p_extract', 'p_fidelity', 'p_relevance', 'p_reader_review', 'q_filter') or not 1 <= len(rows) <= 4:
        raise ValueError('O batch requires 1..4 rows of one supported operation')
    if not scope.get('question_id') or not scope.get('condition'):
        raise ValueError('Missing batch question/condition scope')
    if any(row['scope'] != scope for row in rows):
        raise ValueError('Cross-question or cross-condition batch rejected')
    if len({row['source_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate sentence identity in batch')
    contexts = [{k: row['request'][k] for k in ('question', 'options', 'evidence_task')
                 if k in row['request']} for row in rows]
    if any(c != contexts[0] for c in contexts):
        raise ValueError('Question/options/brief differ within batch')
    if operation in Q4_OPS:
        from .q_v4 import validate_request
        if scope['condition'] != 'evidence_audit_with_options':
            raise ValueError('Wrong Q4 condition scope')
        for row in rows:validate_request(operation,row['request'])
    if operation in Q3_OPS:
        from .q_repair import validate_request
        expected = 'original_question_only' if operation=='q3_filter' else 'evidence_audit_with_options'
        if scope['condition'] != expected:
            raise ValueError('Wrong Q3 condition scope')
        for row in rows:
            validate_request(operation, row['request'])
    if operation in (core.EXTRACT, 'p_extract') and any(set(r['request']) != {'sentence'} for r in rows):
        raise ValueError('Extraction may only read its source sentence')
    if operation == 'p_extract' and scope['condition'] != 'source_only':
        raise ValueError('P extraction requires source-only scope')
    if operation == 'p_fidelity':
        from .p_sentence_filter import ground_schema
        if scope['condition']!='source_only':raise ValueError('Fidelity must not read a question')
        for row in rows:ground_schema(row['request'])
    if operation == 'p_reader_review':
        from .p_sentence_filter import review_schema
        if len(rows)!=1 or scope['condition']!='answer_with_options':
            raise ValueError('P reader review requires one question in final-answer scope')
        review_schema(rows[0]['request'])
    if operation == 'q_filter':
        from .q_evidence import validate_request
        if scope['condition'] != 'original_question_only':
            raise ValueError('Q requires original-question-only scope')
        for row in rows:
            validate_request(row['request'])
            if row['request']['current']['source_id'] != row['source_id']:
                raise ValueError('Q current observation identity mismatch')
    if operation in ('p_filter','p_relevance'):
        from .p_sentence_filter import validate_request
        if scope['condition'] != 'original_question_only':
            raise ValueError('P requires original-question-only scope')
        for row in rows:
            validate_request(row['request'])
    return rows, operation


def messages_for(operation, request):
    if operation.startswith('q4_'):
        from .q_v4 import schema as build_schema, messages
        from .schemas import decoder_schema
        schema=build_schema(operation,request)
        return messages(operation,request),schema,decoder_schema(schema),{}
    if operation.startswith('q3_'):
        from .q_repair import schema as build_schema, PROMPTS as Q3_PROMPTS
        from .schemas import decoder_schema
        schema = build_schema(operation, request)
        wire = request
        if operation == 'q3_option_audit':
            wire = dict(question=request['question'],option_to_check=request['options'][request['option_index']],
                alternative_options=[o for i,o in enumerate(request['options']) if i!=request['option_index']],
                observations=request['observations'])
        # The decoder owns the full ID allowlists. Repeating all IDs in every
        # conditional branch of the displayed schema needlessly inflated input.
        display = schema
        if operation == 'q3_option_audit':
            display = dict(status='supported|contradicted|unresolved',
                witnesses={f'w{i}':'observation source_id or none' for i in range(4)},
                missing_kind='none|action|actor|time|relationship|purpose|ambiguous_source',reason='brief explanation')
        return [dict(role='system',content=Q3_PROMPTS[operation]+'\nOutput format (decoder enforces allowed values and IDs):\n'+json.dumps(display,separators=(',',':'))),
                dict(role='user',content=json.dumps(wire,ensure_ascii=False))],schema,decoder_schema(schema),{}
    if operation == 'q_filter':
        from . import q_evidence as q
        from .schemas import decoder_schema
        q.validate_request(request)
        schema=q.schema()
        return [dict(role='system',content=q.PROMPT),dict(role='user',content=json.dumps(request,ensure_ascii=False))],schema,decoder_schema(schema),{}
    if operation == 'p_reader_review':
        from . import p_sentence_filter as p
        from .schemas import decoder_schema
        schema=p.review_schema(request)
        return [dict(role='system',content=p.REVIEW_PROMPT+'\nRequired output schema:\n'+json.dumps(schema,separators=(',',':'))),
                dict(role='user',content=json.dumps(request,ensure_ascii=False))],schema,decoder_schema(schema),{}
    if operation in ('p_fidelity','p_relevance'):
        from . import p_sentence_filter as p
        from .schemas import decoder_schema
        if operation==p.GROUND:prompt,wire_input,schema=p.ground_wire_contract(request)
        else:prompt,wire_input,schema=p.RELEVANCE_PROMPT,request,p.relevance_schema(request)
        return [dict(role='system',content=prompt),dict(role='user',content=json.dumps(wire_input,ensure_ascii=False))],schema,decoder_schema(schema),{}
    if operation in ('p_filter','p_extract'):
        from . import p_sentence_filter as p
        from .schemas import decoder_schema
        schema = p.schema(request) if operation=='p_filter' else p.extraction_wire_schema()
        prompt=p.FILTER_PROMPT if operation=='p_filter' else p.EXTRACT_PROMPT+'\nRequired output JSON schema (format metadata only):\n'+json.dumps(schema,ensure_ascii=False,separators=(',',':'))
        return ([dict(role='system', content=prompt)] +
                (p.extraction_demonstrations() if operation=='p_extract' else []) +
                [dict(role='user', content=json.dumps(request, ensure_ascii=False))]), schema, decoder_schema(schema), {}
    schema, wire_schema, wire_request, aliases = decoder_contract(operation, request)
    prompt = PROMPTS[operation]
    if aliases:
        prompt += '\nFor groups in identity_text_choices, output text as the supplied __identity_N__ code to select that exact source-bound rendering, or an empty string to omit it. The decoder restores the literal selected rendering. Codes are not evidence. No free text is allowed.'
    messages = [dict(role='system', content=prompt),
                dict(role='user', content=json.dumps(wire_request, ensure_ascii=False))]
    return messages, schema, wire_schema, aliases


def left_pad(sequences, pad_id, device):
    width = max(len(s) for s in sequences)
    ids = torch.full((len(sequences), width), pad_id, dtype=torch.long, device=device)
    mask = torch.zeros_like(ids)
    for i, sequence in enumerate(sequences):
        ids[i, -len(sequence):] = torch.tensor(sequence, dtype=torch.long, device=device)
        mask[i, -len(sequence):] = 1
    return dict(input_ids=ids, attention_mask=mask)


def generate_batch(backend, packet):
    from .backend import deadline_stopping_criteria, decode_structured_text
    rows, operation = validate_packet(packet)
    backend.prepare_vlm()
    model, processor = backend.load('vlm'); tokenizer = processor.tokenizer
    contracts = [messages_for(operation, row['request']) for row in rows]
    sequences = [tokenizer.apply_chat_template(c[0], tokenize=True, add_generation_prompt=True)
                 for c in contracts]
    limit = 1536 if operation.startswith('q4_') else 1024 if operation.startswith('q3_') else 64 if operation=='q_filter' else 8192 if operation=='p_extract' else 256 if operation=='p_fidelity' else 2048 if operation in (core.EXTRACT,'p_extract','p_filter','p_reader_review') else 1024
    width = max(map(len, sequences))
    context = getattr(model.config, 'max_position_embeddings', None) or getattr(
        getattr(model.config, 'text_config', None), 'max_position_embeddings', 32768)
    if width > backend.cfg['models']['max_input_tokens'] or width + limit > context:
        raise ValueError('O batch context overflow; no truncation')
    eos = model.generation_config.eos_token_id
    eos = [eos] if isinstance(eos, int) else eos or []
    pad = tokenizer.pad_token_id
    if pad is None: pad = eos[0]
    encoded = left_pad(sequences, pad, model.device)
    prefixes = [constrained_prefix(backend.enforcer_data, c[2],
        unique_string_arrays=[r['source_id'] for r in row['request']['observations']])
        if operation=='p_reader_review' else constrained_prefix(backend.enforcer_data,c[2])
        for row,c in zip(rows,contracts)]
    def prefix(batch_id, token_ids):
        # Each row owns its schema/parser state, even when all local IDs are U0.
        if any(t in eos for t in token_ids[width:].tolist()):
            return [pad]
        return prefixes[batch_id](0, token_ids)
    kwargs = dict(do_sample=False, num_beams=1, num_return_sequences=1,
                  max_new_tokens=limit, prefix_allowed_tokens_fn=prefix, pad_token_id=pad)
    if backend.deadline is not None:
        kwargs['stopping_criteria'] = deadline_stopping_criteria(backend.deadline)
    backend.model_tokens['vlm'] = backend.model_tokens.get('vlm', 0) + sum(map(len, sequences))
    backend.check_deadline()
    with torch.inference_mode(): generated = model.generate(**encoded, **kwargs)
    backend.check_deadline()
    outputs = []
    for i, row in enumerate(rows):
        schema, wire_schema, aliases = contracts[i][1:]
        tokens = generated[i, width:].tolist()
        end = next((j for j, t in enumerate(tokens) if t in eos), None)
        ended = end is not None
        if ended:
            if any(t not in [pad] + eos for t in tokens[end+1:]):
                raise ValueError('Unexpected non-padding tokens after row EOS')
            tokens = tokens[:end+1]
        raw = tokenizer.decode(tokens, skip_special_tokens=True).strip()
        data = None; wire_data = None; error = None; grounding_audit = None
        try:
            if not ended: raise ValueError('Generation did not terminate normally')
            if operation == 'p_extract':
                from . import p_sentence_filter as p
                data = p.decode_extraction(raw)
            else:data = decode_structured_text(raw)
            core.validate_output(data, wire_schema)
            data = restore_empty_arrays(data, schema)
            for gid, values in aliases.items():
                value = data['groups'][gid]['text']
                if value: data['groups'][gid]['text'] = values[value]
            core.validate_output(data, schema)
            if operation == 'p_extract':
                from . import p_sentence_filter as p
                wire_data = data
                data = p.normalize_extraction(wire_data)
            if operation == 'p_fidelity':
                from . import p_sentence_filter as p
                wire_data = data
                grounding_audit = p.ground_assessment(row['request'],wire_data)
                data = dict(faithful=grounding_audit['faithful'])
        except (ValueError, TypeError, KeyError) as exc: error = str(exc)
        outputs.append(dict(source_id=row['source_id'], scope=row['scope'],
            request_hash=digest(row['request']), input_token_ids=sequences[i],
            prompt=contracts[i][0], data=data, raw=raw, parse_error=error,
            **(dict(wire_data=wire_data) if operation in ('p_extract','p_fidelity') else {}),
            **(dict(grounding_audit=grounding_audit) if operation=='p_fidelity' else {}),
            ended_normally=ended, generated_token_ids=tokens, identity_aliases=aliases,
            decoder=('P-source-reference-review-v1' if operation=='p_reader_review' else 'P-single-decision-batch-v2' if operation in ('p_filter','p_extract','p_fidelity','p_relevance') else
                     'O-source-bound-compose-batch-v2' if operation==core.COMPOSE else 'O-independent-tensor-batch-v1'), max_new_tokens=limit,
            usage=dict(input_tokens=len(sequences[i]), output_tokens=len(tokens))))
    return dict(items=outputs, batch_size=len(rows), scope=packet['scope'],
                operation=operation, padded_input_width=width,
                usage=dict(input_tokens=sum(len(s) for s in sequences),
                           output_tokens=sum(o['usage']['output_tokens'] for o in outputs)))
