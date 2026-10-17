"""Three cache layers with exact targets/scopes, prefix checks and dependency validation."""
from .config import digest
from .state import obj
from pathlib import Path
from .accounting import metered_cpu


def signature(variables, predicates, bindings, goal=None):
    return digest({'variables': variables, 'predicates': sorted(predicates),
                   'bindings': sorted((b['id'], b['revision']) for b in bindings), 'goal': goal})


def cache_key(manifest, frames, model_hash, prompt_hash, spec, target=None, extraction=None, texts=None):
    return digest({'media_hash': manifest['media_hash'], 'cutoff': manifest['cutoff'],
                   'inputs': [{k: f.get(k) for k in ('frame_id', 'pts', 'width', 'height', 'crop', 'content_hash')} for f in frames],
                   'texts': texts or [], 'model': model_hash, 'prompt': prompt_hash,
                   'spec': spec, 'target': target, 'extraction': extraction})


def safe_source(memory, ref, query_scope, seen=None, shared=False):
    seen = set() if seen is None else seen
    if ref in seen:
        return False
    record = memory.get(ref, None)
    if record is None:
        return False
    if shared and any(ref in memory.tables[t] for t in ('bindings', 'requirements', 'derived_claims', 'answers')):
        return False
    if ref in memory.tables['evidence']:
        from .video import file_hash
        for f in record['frame_inputs']:
            if f.get('content_hash'):
                path = f.get('path')
                if not path or not Path(path).is_file() or file_hash(path) != f['content_hash']:
                    return False
        return (record['video_id'] == memory.manifest['video_id']
                and record['interval'][1] <= memory.manifest['cutoff']
                and all(query_scope[0] <= f['pts'] < query_scope[1] for f in record['frame_inputs'])
                and all(query_scope[0] <= s['start'] < s['end'] <= query_scope[1] for s in record['text_spans']))
    dependencies = record.get('evidence_ids', []) + [r for g in record.get('support_sets', []) for r in g]
    return bool(dependencies) and all(safe_source(memory, r, query_scope, seen | {ref}, shared) for r in dependencies)


class CacheManager:
    def __init__(self, memory, query_id, scope, cfg):
        self.memory, self.query_id, self.scope, self.cfg = memory, query_id, scope, cfg
        self.hits = []
        self.requests = []

    @metered_cpu('cache_lookup')
    def lookup(self, key, layer, target=None, extraction=None):
        request = {'layer': layer, 'key': key, 'hit': False}
        self.requests.append(request)
        for entry in self.memory.tables['cache_entries'].values():
            if entry['cache_key'] != key or entry['layer'] != layer or entry['status'] != 'valid':
                continue
            if entry['scope'] == 'query' and entry['query_id'] != self.query_id:
                continue
            if entry['scope'] == 'sequence_shared' and self.cfg['cache']['scope'] != 'sequence_shared':
                continue
            if entry['media_hash'] != self.memory.manifest['media_hash'] or entry['allowed_cutoff'] != self.memory.manifest['cutoff']:
                continue
            if layer != 'inputs' and (not target or entry['target_signature'] != target or entry['extraction_scope_hash'] != digest(extraction)):
                continue
            if any(self.memory.get(r, {}).get('revision') != v for r, v in entry['source_revisions'].items()):
                continue
            shared = entry['scope'] == 'sequence_shared'
            if not all(self.memory.is_valid(r) and safe_source(self.memory, r, self.scope, shared=shared)
                       for r in entry['dependency_ids']):
                continue
            self.hits.append(entry['id'])
            request.update(hit=True, entry_id=entry['id'])
            return entry
        return None

    def statistics(self):
        result = {}
        for layer in ('inputs', 'outputs', 'verified_facts'):
            rows = [r for r in self.requests if r['layer'] == layer]
            hits = sum(r['hit'] for r in rows)
            result[layer] = {'requests': len(rows), 'hits': hits,
                             'hit_rate': hits/len(rows) if rows else None}
        return result

    @metered_cpu('cache_write')
    def save(self, key, layer, input_hash, model_hash, prompt_hash, spec, result_ids,
             dependencies, call_ids, target=None, extraction=None, payload=None):
        if layer != 'inputs' and not target:
            raise ValueError('Model output cache requires a target signature')
        entry = obj(self.memory.new_id(self.query_id, 'cache'), cache_key=key, layer=layer,
                    scope='query', query_id=self.query_id, media_hash=self.memory.manifest['media_hash'],
                    allowed_cutoff=self.memory.manifest['cutoff'], input_hash=input_hash,
                    model_hash=model_hash, prompt_hash=prompt_hash, observation_spec_hash=digest(spec),
                    target_signature=target, extraction_scope_hash=digest(extraction) if extraction is not None else None,
                    result_ids=result_ids, source_revisions={r: self.memory.get(r)['revision'] for r in dependencies},
                    dependency_ids=dependencies, creation_call_ids=call_ids, status='valid', payload=payload)
        self.memory.put('cache_entries', entry)
        return entry
