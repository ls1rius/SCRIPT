"""Read-only preflight for native context and fixed planner output capacity.

No prompt is truncated, no schema decision is changed, and no token limit is
expanded. A worst-case estimate is diagnostic, never an impossibility proof.
"""
import json
import math


def _field(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def native_context(model_config):
    value = _field(model_config, 'max_position_embeddings')
    if value is None:
        value = _field(_field(model_config, 'text_config'), 'max_position_embeddings')
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('Native model context capacity is unknown or invalid; generation was not started')
    return value


def context_preflight(model_config, input_tokens, output_tokens, configured_input_cap, *, label):
    for name, value in [('input_tokens', input_tokens), ('output_tokens', output_tokens),
                        ('configured_input_cap', configured_input_cap)]:
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if name == 'input_tokens' else 1):
            raise ValueError(f'{label} invalid {name}: {value!r}')
    capacity = native_context(model_config)
    audit = dict(input_tokens=input_tokens, output_reserve_tokens=output_tokens,
                 configured_input_cap=configured_input_cap, native_context_tokens=capacity)
    if input_tokens > configured_input_cap:
        raise ValueError(f'{label} input overflow: {input_tokens} > {configured_input_cap}; no truncation')
    if input_tokens + output_tokens > capacity:
        raise ValueError(f'{label} input plus output reserve exceeds model context: '
                         f'{input_tokens} + {output_tokens} > {capacity}; no truncation')
    return audit


def planner_output_preflight(schema, tokenizer, tokenizer_data, output_limit):
    """Audit the flat ASCII decision map used by the L/M/N planner.

    The necessary lower bound counts mandatory JSON characters and divides by
    the longest tokenizer piece, then reserves EOS. This intentionally weak
    bound is safe even when tokenization crosses field boundaries. The familiar
    sum of independently tokenized keys/longest values is only an estimate;
    exceeding it never rejects a plan that could fit with shorter decisions.
    """
    required = schema.get('required', [])
    properties = schema.get('properties', {})
    if (schema.get('type') != 'object' or not required or set(required) != set(properties)
            or len(required) != len(set(required))):
        return dict(supported=False, reason='not a complete flat planner decision map')
    encoded_keys = []
    choices = []
    for key in required:
        field = properties[key]
        values = field.get('enum', [])
        if (not isinstance(key, str) or not key.isascii() or field.get('type') != 'string'
                or not values or any(not isinstance(v, str) or not v.isascii() for v in values)):
            return dict(supported=False, reason='planner keys and choices must be finite ASCII strings')
        encoded_keys.append(json.dumps(key, ensure_ascii=True))
        choices.append([json.dumps(v, ensure_ascii=True) for v in values])
    # Encodings are cached across the many identical per-record value enums.
    sizes = {}
    def token_size(text):
        if text not in sizes:
            sizes[text] = len(tokenizer.encode(text, add_special_tokens=False))
        return sizes[text]
    estimate = sum(token_size(key) + max(map(token_size, values)) + 4
                   for key, values in zip(encoded_keys, choices)) + 128
    minimum_characters = 2 + max(0, len(required) - 1) + sum(
        len(key) + 1 + min(map(len, values)) for key, values in zip(encoded_keys, choices))
    longest_piece = getattr(tokenizer_data, '_planner_max_piece_characters', None)
    if longest_piece is None:
        rows = getattr(tokenizer_data, 'regular_tokens', ())
        # A word-boundary space may be handled separately by a tokenizer.
        longest_piece = max((len(row[1]) + 1 for row in rows), default=0)
        if longest_piece:
            tokenizer_data._planner_max_piece_characters = longest_piece
    lower = math.ceil(minimum_characters / longest_piece) + 1 if longest_piece else None
    audit = dict(supported=True, records=len(required), output_limit_tokens=output_limit,
                 necessary_minimum_characters=minimum_characters,
                 maximum_token_piece_characters=longest_piece or None,
                 necessary_output_token_lower_bound=lower,
                 worst_case_token_estimate=estimate,
                 worst_case_estimate_fits=estimate <= output_limit,
                 worst_case_estimate_is_proof=False)
    if lower is not None and lower > output_limit:
        raise ValueError('Planner output capacity impossible: necessary token lower bound '
                         f'{lower} > {output_limit}; no truncation or token-limit expansion')
    return audit
