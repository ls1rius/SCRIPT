"""Explicit ordered experiment cache; no query bindings or answers are shared."""
from copy import deepcopy
import json
from pathlib import Path

from .cache import safe_source
from .config import digest
from .state import Store, write_json


class SequenceCache:
    def __init__(self, manifest_path, questions, cfg):
        manifest = json.loads(Path(manifest_path).read_text())
        required = {'video_id', 'cutoff', 'question_order', 'cache_directory'}
        if required-set(manifest):
            raise ValueError('Sequence manifest requires video_id, cutoff, question_order and cache_directory')
        order = [q.get('question_id', q.get('id')) for q in questions]
        if order != manifest['question_order'] or any(q['video_id'] != manifest['video_id'] for q in questions):
            raise ValueError('Sequence questions do not match the preregistered video/order')
        self.manifest, self.cfg = manifest, cfg
        self.root = Path(manifest['cache_directory'])
        self.root.mkdir(parents=True, exist_ok=True)
        self.protocol_hash = digest([manifest, cfg])
        protocol = self.root / 'protocol.json'
        if protocol.exists() and json.loads(protocol.read_text())['hash'] != self.protocol_hash:
            raise ValueError('Shared cache belongs to a different sequence/configuration')
        write_json(protocol, {'hash': self.protocol_hash, 'manifest': manifest})

    def read(self, base, initial=None):
        if base.manifest['video_id'] != self.manifest['video_id'] or base.manifest['cutoff'] != self.manifest['cutoff']:
            raise ValueError('Shared cache prefix or video mismatch')
        memory = deepcopy(initial or base)
        if not (self.root / 'state.sqlite3').exists():
            return memory
        store = Store(self.root)
        try:
            shared, metadata = store.load()
        finally:
            store.close()
        if metadata['media_hash'] != base.manifest['media_hash'] or metadata['config_hash'] != digest(self.cfg):
            raise ValueError('Shared media or model configuration mismatch')
        for table in ('evidence', 'occurrences', 'entities', 'identity_links', 'claims', 'cannot_links', 'observations', 'cache_entries'):
            for r in shared.tables[table].values():
                if r['id'] not in memory.tables[table]:
                    memory.put(table, r)
        return memory

    def commit(self, query_directory, base):
        store = Store(query_directory)
        try:
            work, metadata = store.load()
        finally:
            store.close()
        shared = deepcopy(base)
        scope = [0., base.manifest['cutoff']]
        selected = set()
        for table in ('claims', 'identity_links', 'cannot_links'):
            for r in work.tables[table].values():
                if work.is_valid(r['id']) and safe_source(work, r['id'], scope, shared=True):
                    selected.add(r['id'])
                    selected.update(r.get('evidence_ids', []))
                    if table == 'claims':
                        selected.update(v for v in r['roles'].values() if v in work.tables['occurrences'])
                    elif table == 'identity_links':
                        selected.update((r['occurrence_id'], r['entity_id']))
                    else:
                        selected.update(r['occurrence_ids'])
        for o in work.tables['occurrences'].values():
            if o['id'] in selected:
                selected.update(o['evidence_ids'])
        for table in ('evidence', 'occurrences', 'entities', 'identity_links', 'claims', 'cannot_links'):
            for r in work.tables[table].values():
                if r['id'] in selected and r['id'] not in shared.tables[table]:
                    record = deepcopy(r)
                    if table == 'entities':
                        record['confirmed_occurrence_ids'] = [o for o in record['confirmed_occurrence_ids'] if o in selected]
                        record['candidate_link_ids'] = []
                    if table == 'occurrences':
                        record['entity_candidate_ids'] = [e for e in record['entity_candidate_ids'] if e in selected]
                    shared.put(table, record)
        for entry in work.tables['cache_entries'].values():
            if entry['layer'] not in ('inputs', 'verified_facts'):
                continue
            if not all(d in selected or d in shared.tables['evidence'] for d in entry['dependency_ids']):
                continue
            if not all(safe_source(work, d, scope, shared=True) for d in entry['dependency_ids']):
                continue
            record = deepcopy(entry)
            record['scope'], record['query_id'] = 'sequence_shared', None
            if record['layer'] == 'verified_facts':
                # Facts are reinterpreted per question. No previous target cache can resolve a new need.
                record['target_signature'] = None
            shared.put('cache_entries', record)
        store = Store(self.root)
        try:
            store.checkpoint(shared, {'media_hash': base.manifest['media_hash'], 'config_hash': digest(self.cfg),
                                      'creation_history': metadata.get('state', {}).get('progress_history', [])})
        finally:
            store.close()
