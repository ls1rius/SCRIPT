"""Frozen real-return replay. Missing observations fail closed instead of reading new video."""
from copy import deepcopy
import json
from pathlib import Path
import time

from .budget import ToolFailure
from .config import digest
from .video import file_hash

PERCEPTION = {'inspect', 'decode', 'features', 'detect', 'track', 'supplement', 'caption',
              'extract', 'observe', 'verify_claim', 'verify_claims', 'verify_identity', 'coverage'}


def canonical_inputs(operation, inputs):
    identifiers = []
    def collect(value):
        if isinstance(value, dict):
            if isinstance(value.get('id'), str) and value['id'] not in identifiers:
                identifiers.append(value['id'])
            for key in sorted(value):
                collect(value[key])
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(inputs)
    aliases = {ref: f'REF{i}' for i, ref in enumerate(identifiers)}
    ignored = {'output', 'crop_directory', 'path', 'crop_ref', 'crop_refs', 'appearance_feature_ref',
               'call_id', 'observer_config_hash', 'input_hash', 'output_hash', 'prototype_refs',
               'actual_cost', 'expected_cost', 'cumulative_online_cost', 'reservation', 'cost_summary',
               'replay_origin', '_replay_origin', 'model_hash', 'prompt_hash'}
    if operation in PERCEPTION:
        ignored |= {'goal', 'question'}
    def normalize(value):
        if isinstance(value, dict):
            if 'media_hash' in value and 'frames' in value:
                value = {k: value[k] for k in ('media_hash', 'cutoff', 'frames', 'subtitle_hash') if k in value}
            return {k: normalize(v) for k, v in value.items() if k not in ignored}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return aliases.get(value, value) if isinstance(value, str) else value
    return digest({'operation': operation, 'inputs': normalize(inputs)}), aliases


def replace_refs(value, mapping):
    if isinstance(value, dict):
        return {mapping.get(k, k): replace_refs(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_refs(v, mapping) for v in value]
    return mapping.get(value, value) if isinstance(value, str) else value


class ReplayBackend:
    @property
    def requires_vlm_warmup(self):
        return not self.replay_only and getattr(self.live, 'requires_vlm_warmup', False) is True

    def __init__(self, live, source, replay_only=False):
        self.live, self.source, self.replay_only = live, Path(source), replay_only
        log = self.source / 'calls.jsonl'
        if not log.exists():
            raise ValueError('Replay source must be a completed real query directory with calls.jsonl')
        if (self.source / 'replay_manifest.json').exists():
            raise ValueError('Replay cannot masquerade as fresh recorded observations')
        self.records, frozen_files = {}, {}
        for line in log.read_text().splitlines():
            call = json.loads(line)
            ref = call.get('output_ref')
            if not ref:
                continue
            path = self.source / 'raw_calls' / Path(ref).name
            if not path.exists():
                raise ValueError(f'Replay raw return missing: {path}')
            raw = json.loads(path.read_text())
            key, aliases = canonical_inputs(call['operation'], raw['inputs'])
            self.records.setdefault(key, []).append((path, file_hash(path), raw, call, aliases))
            frozen_files[str(path)] = file_hash(path)
        if not self.records:
            raise ValueError('Replay source contains no real tool returns')
        self.frozen_hash = digest(frozen_files)
        artifacts = self.source.parent / 'artifacts.json'
        self.source_prompts = json.loads(artifacts.read_text()).get('prompts', {}) if artifacts.exists() else {}
        self.used = []
        self.positions = {}

    def call(self, operation, inputs, timeout):
        if operation not in PERCEPTION and not self.replay_only:
            return self.live.call(operation, inputs, timeout)
        started, cpu_started = time.monotonic(), time.process_time()
        key, aliases = canonical_inputs(operation, inputs)
        matches = self.records.get(key)
        if not matches:
            raise ToolFailure(f'Frozen observation replay miss: {operation}; no live perception fallback')
        position = min(self.positions.get(key, 0), len(matches)-1)
        self.positions[key] = position+1
        path, expected_hash, raw, call, old_aliases = matches[position]
        if file_hash(path) != expected_hash:
            raise ToolFailure('Frozen replay return changed')
        if raw['error']:
            if 'TimeoutError:' in raw['error']:
                raise TimeoutError('Replayed timeout: '+raw['error'])
            if 'ConnectionError:' in raw['error']:
                raise ConnectionError('Replayed connection failure: '+raw['error'])
            raise ToolFailure('Replayed tool failure: '+raw['error'])
        mapping = {old: new for old, alias in old_aliases.items() for new, current in aliases.items() if alias == current}
        output = replace_refs(deepcopy(raw['output']), mapping)
        # Actual input files are still checked, including full-frame/crop fingerprints.
        for frame in output.get('data', []) if operation == 'decode' else inputs.get('frames', []):
            if isinstance(frame, dict) and frame.get('content_hash'):
                if not Path(frame['path']).is_file() or file_hash(frame['path']) != frame['content_hash']:
                    raise ToolFailure('Replay pixel input changed or is missing')
        if time.monotonic()-started > timeout:
            raise TimeoutError('Replay IO exceeded the reserved budget')
        output['replay'] = {'source': str(path), 'source_hash': expected_hash,
                            'source_processing_seconds': call['actual_cost'],
                            'source_model_hash': call.get('model_hash'),
                            'source_prompt_hash': self.source_prompts.get(operation),
                            'source_usage': output.get('usage', {}), 'manifest_hash': self.frozen_hash}
        output['usage'] = {'cpu_seconds': time.process_time()-cpu_started, 'gpu_resident_seconds': 0.,
                           'model_frame_views': output.get('usage', {}).get('model_frame_views', []),
                           'context_projection': output.get('usage', {}).get('context_projection')}
        if isinstance(output.get('data'), dict) and operation in ('verify_claim', 'verify_claims', 'verify_identity'):
            output['data']['_replay_origin'] = {k: output['replay'][k] for k in
                ('source', 'source_hash', 'source_model_hash', 'source_prompt_hash')}
        self.used.append({'operation': operation, 'source': str(path), 'input_hash': key})
        return output

    def manifest(self):
        return {'mode': 'recorded_calls' if self.replay_only else 'fixed_observation_replay',
                'source': str(self.source.resolve()), 'source_hash': self.frozen_hash, 'calls': self.used,
                'cost_rule': 'Measured replay IO and live planning; original perception costs reported separately.'}
