"""Exact-input text-tool reuse inside one experiment and one method."""
from copy import deepcopy
from functools import lru_cache
import fcntl
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import uuid

from .config import digest

OPERATIONS = frozenset(('temporal_summary', 'caption_temporal_observe', 'scene_scope',
    'temporal_review', 'scene_correspondence_lineage', 'adaptive_next', 'adaptive_extract',
    'transition_next', 'transition_extract', 'optimized_plan', 'optimized_extract', 'optimized_events',
    'clip_text'))
SOFTWARE = ('torch', 'transformers', 'numpy', 'lm-format-enforcer')
VERSION = 1


@lru_cache(maxsize=16)
def source_identity(root):
    # Meter/cache plumbing and config loading do not alter the backend's model
    # input or decoder. Actual config values are independently part of each key.
    ignored = {'budget.py', 'persistent_cache.py', 'directory_cache.py', 'config.py', 'temporal_pipeline.py'}
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).glob('*.py')) if p.name not in ignored}


def scope_for(raw_directory):
    parts = Path(raw_directory).parts
    if 'arms' in parts:
        return parts[parts.index('arms')+1]
    if 'refill' in parts:
        tail = parts[parts.index('refill')+1:]
        if tail and tail[0] in ('adaptive_partial', 'adaptive_scheme4', 'optimized_scheme4'):
            return tail[0]
        return 'optimized_scheme4'
    if 'directories' in parts:
        return 'preprocessing'
    return 'setup'


def effective_config(cfg):
    cfg = deepcopy(cfg)
    cfg.get('models', {}).get('revisions', {}).pop('@online', None)
    for k in ('persistent_directory', 'persistent_scope'):
        cfg.get('cache', {}).pop(k, None)
    return cfg


def checked_output(operation, inputs, output):
    if operation == 'clip_text':
        vectors = output.get('vectors')
        if (not isinstance(vectors, list) or len(vectors) != len(inputs.get('texts', []))
                or not vectors or not vectors[0]):
            raise ValueError('Invalid cached text embeddings')
        width = len(vectors[0])
        if any(len(v) != width or any(not isinstance(x, (int, float)) or not math.isfinite(x)
                                    for x in v) for v in vectors):
            raise ValueError('Non-finite or inconsistent cached embeddings')
        return
    from .schemas import output_schema, restore_empty_arrays, validate_output
    from .backend import decode_structured_text
    value = deepcopy(output.get('data'))
    if isinstance(value, str):
        value = decode_structured_text(value)
    if isinstance(value, dict) and set(value) == {'data'}:
        value = value['data']
    schema = output_schema(operation, inputs)
    validate_output(restore_empty_arrays(value, schema), schema)


class TextCache:
    def __init__(self, cfg, raw_directory=None, *, source_root=None, software=None, scope=None):
        self.cfg = cfg
        root = cfg.get('cache', {}).get('persistent_directory')
        self.root = Path(root) if root else None
        self.scope = scope or cfg.get('cache', {}).get('persistent_scope') or scope_for(raw_directory or '')
        self.source_root = str(source_root or Path(__file__).resolve().parent)
        self.software = software

    def key(self, operation, inputs):
        if self.root is None or operation not in OPERATIONS or inputs.get('frames') or inputs.get('images'):
            return None
        models = self.cfg['models']
        needed = 'visual_encoder' if operation == 'clip_text' else 'vlm'
        if not models.get('revisions', {}).get(needed):
            return None
        if operation != 'clip_text' and models['generation'].get('do_sample', False):
            return None
        if self.software is None:
            self.software = {name: importlib.metadata.version(name) for name in SOFTWARE}
        from .context import model_view
        from .prompts import CONTRACTS, SYSTEM
        from .schemas import output_schema
        # Bookkeeping is omitted only by the same projection used by the real
        # backend. Caption text, timestamps, requested fields and entity IDs stay.
        semantic = inputs if operation == 'clip_text' else model_view(inputs)
        schema = None if operation == 'clip_text' else output_schema(operation, inputs)
        return digest(dict(version=VERSION, scope=self.scope, operation=operation,
            inputs=semantic, schema=schema, config=effective_config(self.cfg),
            contract=[SYSTEM, CONTRACTS.get(operation)], software=self.software,
            source=source_identity(self.source_root)))

    def path(self, key):
        return self.root/self.scope/key[:2]/(key+'.json')

    def get(self, operation, inputs):
        key = self.key(operation, inputs)
        if key is None:
            return None
        path = self.path(key)
        try:
            entry = json.loads(path.read_text())
            body = entry['body']
            if (entry['checksum'] != digest(body) or body['key'] != key
                    or body['operation'] != operation or body['scope'] != self.scope
                    or not math.isfinite(body['source_seconds']) or body['source_seconds'] < 0):
                raise ValueError('Cache integrity mismatch')
            checked_output(operation, inputs, body['output'])
            return body
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError):
            # A corrupt entry is never evidence. Leave it available for audit.
            return None

    def put(self, operation, inputs, output, seconds, origin):
        key = self.key(operation, inputs)
        if key is None or not math.isfinite(seconds) or seconds < 0:
            return False
        try:
            checked_output(operation, inputs, output)
        except (ValueError, KeyError, TypeError):
            return False
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.get(operation, inputs) is not None:
                return False
            clean = deepcopy(output)
            clean.get('usage', {}).pop('tool_cache', None)
            body = dict(key=key, operation=operation, scope=self.scope, output=clean,
                        source_seconds=seconds, origin=origin)
            temp = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            temp.write_text(json.dumps(dict(body=body, checksum=digest(body)), ensure_ascii=False,
                                       allow_nan=False)+'\n')
            temp.replace(path)
        return True
