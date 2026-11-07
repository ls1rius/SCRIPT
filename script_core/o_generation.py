"""O-only text generation; leaves all existing N/M operations unchanged."""
import json
from copy import deepcopy
import torch
from . import o_sentence_filter as core
from .schemas import decoder_schema, restore_empty_arrays


def decoder_contract(operation,request):
    """Use finite ASCII choice codes for every source-bound composition group."""
    canonical=core.schema(operation,request)
    wire=decoder_schema(canonical);inputs=deepcopy(request);aliases={}
    if operation==core.COMPOSE:
        for gid,group in canonical['properties']['groups']['properties'].items():
            values=group['properties']['text'].get('enum',[])
            if values:
                aliases[gid]={f'__identity_{i}__':v for i,v in enumerate(values) if v}
                wire['properties']['groups']['properties'][gid]['properties']['text']['enum']=(
                    [''] if '' in values else [])+list(aliases[gid])
    if aliases:inputs['identity_text_choices']=aliases
    return canonical,wire,inputs,aliases


def constrained_prefix(tokenizer_data,schema,*,unique_string_arrays=None):
    from lmformatenforcer import JsonSchemaParser
    from lmformatenforcer.integrations.transformers import build_transformers_prefix_allowed_tokens_fn
    from .backend import navigation_tokenizer_data
    from .json_unicode_guard import ScalarEscapeParser, tokenizer_data as scalar_data
    parser=JsonSchemaParser(schema)
    guarded=ScalarEscapeParser(parser)
    if unique_string_arrays is not None:
        from .json_unique_array_guard import UniqueStringArrayParser
        guarded=UniqueStringArrayParser(guarded,unique_string_arrays)
    prefix=build_transformers_prefix_allowed_tokens_fn(scalar_data(navigation_tokenizer_data(tokenizer_data)),guarded)
    parser.context.alphabet_without_quotes=parser.config.alphabet.replace('"','')
    parser.config.force_json_field_order=True;parser.config.max_consecutive_whitespaces=1
    return prefix


def generate(backend,operation,request):
    from .backend import deadline_stopping_criteria, decode_structured_text
    backend.prepare_vlm()
    model,processor=backend.load('vlm');tokenizer=processor.tokenizer
    prompt={core.EXTRACT:core.EXTRACT_PROMPT,core.FILTER:core.FILTER_PROMPT,core.COMPOSE:core.COMPOSE_PROMPT,core.FILTER_PLAN:core.FILTER_PLAN_PROMPT}[operation]
    schema,wire_schema,wire_request,aliases=decoder_contract(operation,request)
    if aliases:prompt+='\nFor groups in identity_text_choices, output text as the supplied __identity_N__ code to select that exact source-bound rendering, or an empty string to omit it. The decoder restores the literal selected rendering. Codes are not evidence. No free text is allowed.'
    messages=[dict(role='system',content=prompt),dict(role='user',content=json.dumps(wire_request,ensure_ascii=False))]
    encoded=tokenizer.apply_chat_template(messages,tokenize=True,add_generation_prompt=True,
        return_dict=True,return_tensors='pt').to(model.device)
    count=int(encoded.input_ids.shape[1]);limit={core.EXTRACT:2048,core.FILTER:1024,core.COMPOSE:1024,core.FILTER_PLAN:2048}[operation]
    maximum=backend.cfg['models']['max_input_tokens']
    context=getattr(model.config,'max_position_embeddings',None) or getattr(getattr(model.config,'text_config',None),'max_position_embeddings',32768)
    if count>maximum or count+limit>context:raise ValueError('O context overflow; no truncation')
    prefix=constrained_prefix(backend.enforcer_data,wire_schema)
    kwargs=dict(do_sample=False,max_new_tokens=limit,prefix_allowed_tokens_fn=prefix)
    if backend.deadline is not None:kwargs['stopping_criteria']=deadline_stopping_criteria(backend.deadline)
    backend.model_tokens['vlm']=backend.model_tokens.get('vlm',0)+count
    backend.check_deadline()
    with torch.inference_mode():generated=model.generate(**encoded,**kwargs)
    backend.check_deadline()
    tokens=generated[0,count:];raw=tokenizer.decode(tokens,skip_special_tokens=True).strip()
    eos=model.generation_config.eos_token_id;eos=[eos] if isinstance(eos,int) else eos or []
    ended=bool(len(tokens)) and int(tokens[-1]) in eos
    data=None;error=None
    try:
        if not ended:raise ValueError('Generation did not terminate normally')
        data=decode_structured_text(raw)
        core.validate_output(data,wire_schema)
        data=restore_empty_arrays(data,schema)
        for gid,values in aliases.items():
            value=data['groups'][gid]['text']
            if value:data['groups'][gid]['text']=values[value]
        core.validate_output(data,schema)
    except (ValueError,TypeError) as exc:error=str(exc)
    return dict(data=data,raw=raw,parse_error=error,ended_normally=ended,
        generated_token_ids=tokens.tolist(),decoder='O-source-bound-compose-alias-v3' if operation==core.COMPOSE else 'O-complete-unicode-identity-alias-v2',max_new_tokens=limit,
        identity_aliases=aliases,
        usage=dict(input_tokens=count,output_tokens=len(tokens)))
