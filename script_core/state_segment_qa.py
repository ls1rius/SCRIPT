"""Online D input using the same C projection and segment renderer as evaluation."""
from copy import deepcopy
import json

from .accounting import metered_cpu
from .complete_memory import source_bundle
from .semantic_memory import validate_coverage
from .state_segment_memory import materialize, render


def sources(packet, selected, context):
    # Visual observations remain in their original final-QA block. Only the
    # already-read caption Description is normalized, as in the controlled D run.
    return source_bundle(packet, dict(context=context, selected_seconds=selected), [])


@metered_cpu('state_segment_input_projection')
def project(original, packet, selected, memory, bundle, max_gap_seconds=2, *, final=False):
    from script.controlled_memory_io import fixed, parts, semantic_input

    validate_coverage(bundle, memory)
    rows = [dict(caption_id=packet['captions'][i]['id'], frame_index=i,
                 sample_interval=deepcopy(packet['captions'][i]['interval']),
                 evidence=dict(original_text=packet['captions'][i]['text']))
            for i in sorted(selected)]
    prefix, _, suffix = parts(original)
    lossless = deepcopy(original)
    lossless['prompts'][0] = prefix + json.dumps(dict(observations=rows), ensure_ascii=False) + suffix
    semantic, coverage = semantic_input(original, lossless, memory, bundle)
    payload = json.loads(parts(semantic)[1])
    segments = materialize(payload, max_gap_seconds)
    description, audit = render(segments)
    result = deepcopy(original)
    result['prompts'][0] = prefix + description + suffix
    if fixed(result) != fixed(original):
        raise ValueError('D changed a field outside Description')
    return result, segments, dict(mode='state_segments', source_coverage=coverage, **audit)
