"""Explicit model views: preserve facts and frames, omit storage bookkeeping."""
import math


BOOKKEEPING = {'features', 'appearance_feature_ref', 'prototype_refs', 'crop_refs', 'crop_inputs',
               'path', 'input_hash', 'output_hash', 'observer_config_hash', 'content_hash',
               'model_hash', 'prompt_hash', 'call_ids', 'call_id', 'config_hash', 'schema_version',
               'verification_history'}


def operation_inputs(operation, inputs):
    if operation == 'assess':
        inputs = assessment_inputs(inputs)
    if operation in ('assess', 'gate', 'answer'):
        inputs = decision_inputs(inputs)
    return inputs


def assessment_inputs(inputs):
    """Only localized roles with a fact/binding can participate in assessment.

    Preserve every claim. Unreferenced detection history is still in storage, but
    cannot establish a binding or an identity correction for these claims.
    """
    references = {ref for c in inputs.get('claims', []) for role, ref in c.get('roles', {}).items() if role != 'content'}
    references.update(ref for b in inputs.get('bindings', []) for ref in b.get('anchor_occurrence_ids', []))
    result = dict(inputs)
    if 'occurrences' in inputs:
        result['occurrences'] = [o for o in inputs['occurrences'] if o['id'] in references]
        omitted = len(inputs['occurrences'])-len(result['occurrences'])
        result['context_selection'] = dict(inputs.get('context_selection', {}))
        result['context_selection']['unreferenced_occurrences_omitted'] = (
            result['context_selection'].get('unreferenced_occurrences_omitted', 0)+omitted)
    if 'identity_links' in inputs:
        result['identity_links'] = [l for l in inputs['identity_links'] if l['occurrence_id'] in references]
    entities = {l['entity_id'] for l in result.get('identity_links', [])}
    entities.update(b['entity_id'] for b in inputs.get('bindings', []) if b.get('entity_id'))
    if 'entities' in inputs:
        result['entities'] = [e for e in inputs['entities'] if e['id'] in entities]
    return result


def model_view(inputs):
    frames = {f['frame_id']: f for f in inputs.get('frames', [])}

    def compact(value):
        if isinstance(value, dict):
            # The boxes are drawn with occurrence IDs on *all* supplied frames.
            # Repeating every historical box in text can dwarf the images themselves.
            if 'boxes' in value and 'category' in value:
                result = {k: compact(value[k]) for k in
                          ('id', 'category', 'description', 'visible_intervals', 'aliases') if k in value}
                result['visible_frame_ids'] = list(dict.fromkeys(
                    b['frame_id'] for b in value['boxes'] if b['frame_id'] in frames))
                return result
            if 'frame_inputs' in value and 'source_type' in value:
                result = {k: compact(value[k]) for k in
                          ('id', 'interval', 'source_type', 'observation', 'text_spans', 'scene_ids') if k in value}
                result['actual_pts'] = [f['pts'] for f in value['frame_inputs']]
                return result
            return {k: compact(v) for k, v in value.items() if k not in BOOKKEEPING and k != 'frames'}
        if isinstance(value, list):
            return [compact(v) for v in value]
        return value

    result = compact(inputs)
    if inputs.get('options'):
        # Keep each ID beside its text; parallel arrays caused observed ID drift.
        result['options'] = [{'option_id': str(i), 'text': text}
                             for i, text in enumerate(inputs['options'])]
        from .query_policy import option_distinctions
        result['option_distinctions'] = option_distinctions(inputs['options'])
    return result


def decision_inputs(inputs):
    """Keep every semantic fact and source, omit repeated proof histories.

    The controller validates the full support graph. Text decisions do not need
    every historical support set or each frame's copy of verifier metadata.
    """
    result = dict(inputs)
    fields = {
        'claims': ('id', 'predicate', 'fact_type', 'roles', 'interval', 'observed_at', 'source_type',
                   'description', 'evidence_ids', 'status'),
        'requirements': ('id', 'predicate', 'roles', 'scope', 'quantifier', 'answer_slot', 'status'),
        'bindings': ('id', 'variable', 'entity_id', 'anchor_occurrence_ids', 'status'),
        'coverage': ('id', 'interval', 'target_requirement_ids', 'level', 'visibility', 'result'),
    }
    for table, keep in fields.items():
        if table in result:
            result[table] = [{k: row[k] for k in keep if k in row} for row in result[table]]
    for projected, original in zip(result.get('claims', []), inputs.get('claims', [])):
        verification = original.get('verification', {})
        if verification:
            projected['verification'] = {k: list(dict.fromkeys(verification[k])) if isinstance(verification[k], list) else verification[k]
                for k in ('result', 'distinguishing_facts', 'unresolved_conflicts', 'process_visible', 'whole_claim_supported') if k in verification}
    return result


def encode_with_frame_budget(processor, text, images, max_tokens):
    """Fit pixels to the exact token allowance; never drop frames or truncate text.

    Return the actual processor grids for provenance and target-size checks. If text
    alone cannot fit, the caller must explicitly batch its records instead.
    """
    from PIL import Image
    originals = images
    current = images
    for _ in range(10):
        encoded = processor(text=[text], images=current or None, padding=True, return_tensors='pt')
        tokens = encoded.input_ids.shape[-1]
        grids = encoded.get('image_grid_thw')
        merge = getattr(processor.image_processor, 'merge_size', 2) if current else 2
        patch = getattr(processor.image_processor, 'patch_size', 14) if current else 14
        views = ([{'width': int(g[2])*patch, 'height': int(g[1])*patch} for g in grids]
                 if grids is not None else [{'width': im.width, 'height': im.height} for im in current])
        if tokens <= max_tokens:
            return encoded, views
        image_tokens = sum(int(g[0])*int(g[1])*int(g[2])//(merge*merge) for g in grids) if grids is not None else 0
        text_tokens = tokens-image_tokens
        if not current or (image_tokens and text_tokens >= max_tokens):
            raise ValueError(f'Context overflow ({tokens}); text records require explicit batching')
        factor = min(.8, math.sqrt(max(0., (max_tokens-text_tokens)*.9)/image_tokens)) if image_tokens else .65
        sizes = [(max(56, int(im.width*factor)//28*28), max(56, int(im.height*factor)//28*28)) for im in current]
        if sizes == [(im.width, im.height) for im in current]:
            break
        current = [original.resize(size, Image.Resampling.LANCZOS) for original, size in zip(originals, sizes)]
    raise ValueError(f'Context overflow ({tokens}); all frames retained at minimum resolution, explicit batching required')


def encode_caption_context(processor, render, inputs, max_tokens):
    """Fit whole unverified caption windows while retaining every verified fact."""
    from .query_policy import spread_order
    candidate = dict(inputs)
    context = dict(inputs.get('coarse_context') or {})
    original = list(context.get('windows', []))
    selected = original
    while True:
        context = dict(context, windows=selected, selected_windows=len(selected),
                       omitted_windows=context.get('eligible_windows', len(original))-len(selected))
        candidate['coarse_context'] = context
        try:
            encoded, _ = encode_with_frame_budget(processor, render(candidate), [], max_tokens)
            return encoded, {'requested_windows': len(original), 'selected_window_ids': [w['id'] for w in selected],
                             'omitted_window_ids': [w['id'] for w in original if w not in selected]}
        except ValueError as exc:
            if inputs.get('summary_graph'):
                # An experimental supplement must not silently displace the
                # baseline descriptions. Caller records failure/falls back.
                raise
            if 'Context overflow' not in str(exc) or not selected:
                raise
            keep = len(selected)//2
            order = spread_order([(w['interval'][0]+w['interval'][1])/2 for w in selected])
            chosen = ({len(selected)//2} if keep == 1 else {0, len(selected)-1} if keep == 2 else set(order[:keep]))
            selected = [w for i, w in enumerate(selected) if i in chosen]
