"""Versioned object store, provenance checks, AND/OR dependencies and transactions."""
from contextlib import contextmanager
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3
import fcntl

from .config import digest
from .accounting import metered_cpu

PREDICATE_ROLES = {
    'at': ('object', 'location'), 'hold': ('actor', 'object'),
    'pick_up': ('actor', 'object'), 'place': ('actor', 'object', 'destination'),
    'transfer': ('actor', 'recipient', 'object'), 'enter': ('actor', 'location'),
    'leave': ('actor', 'location'), 'talk_to': ('speaker', 'listener'),
    'speech_claim': ('speaker', 'content'), 'open': ('actor', 'object'),
    'close': ('actor', 'object'), 'interact': ('actor', 'object'), 'other': (),
}
DYNAMIC = {'pick_up', 'place', 'transfer', 'enter', 'leave', 'open', 'close'}
TABLES = ('scenes', 'windows', 'mentions', 'summaries', 'evidence', 'coverage',
          'occurrences', 'entities', 'identity_links', 'claims', 'requirements', 'bindings',
          'derived_claims', 'branches', 'candidate_queue', 'observations', 'build_records',
          'cache_entries', 'summary_corrections', 'patches', 'tasks', 'local_results',
          'stop_decisions', 'answers', 'cannot_links')
VALID_STATUSES = {'supported', 'confirmed', 'satisfied'}
ENUMS = {
    'claims': {'supported', 'unresolved', 'refuted', 'invalidated'},
    'bindings': {'candidate', 'confirmed', 'conflicted', 'revoked'},
    'identity_links': {'candidate', 'confirmed', 'conflicted', 'revoked'},
    'requirements': {'unresolved', 'satisfied', 'conflicted'},
    'derived_claims': {'supported', 'unresolved', 'invalidated'},
    'build_records': {'running', 'complete', 'partial', 'failed'},
    'cache_entries': {'valid', 'stale', 'blocked'},
    'branches': {'active', 'stalled', 'refuted', 'completed'},
    'candidate_queue': {'pending', 'visited', 'deferred', 'invalidated'},
    'patches': {'proposed', 'committed', 'rejected'},
    'summary_corrections': {'supported', 'invalidated'},
}
REQUIRED = {
    'evidence': ('video_id', 'scene_ids', 'interval', 'source_type', 'frame_inputs', 'text_spans',
                 'observation', 'input_hash', 'output_hash', 'observer_config_hash', 'call_id'),
    'claims': ('scene_ids', 'predicate', 'roles', 'interval', 'observed_at', 'status', 'source_type',
               'evidence_ids', 'counterevidence_ids', 'support_sets', 'verification', 'duplicate_group'),
    'occurrences': ('scene_id', 'category', 'aliases', 'visible_intervals', 'boxes', 'crop_refs',
                    'appearance_feature_ref', 'description', 'evidence_ids', 'entity_candidate_ids', 'stable_track'),
    'entities': ('category', 'aliases', 'descriptors', 'confirmed_occurrence_ids', 'candidate_link_ids', 'prototype_refs'),
    'identity_links': ('occurrence_id', 'entity_id', 'status', 'retrieval_score', 'evidence_ids', 'cannot_link_refs', 'verification'),
    'requirements': ('predicate', 'roles', 'scope', 'quantifier', 'prerequisite_ids', 'acceptable_modalities',
                     'answer_slot', 'support_sets', 'status'),
    'bindings': ('variable', 'entity_id', 'anchor_occurrence_ids', 'support_sets', 'status'),
    'coverage': ('interval', 'target_requirement_ids', 'evidence_ids', 'actual_pts', 'max_sample_gap',
                 'min_target_short_side', 'visibility', 'level', 'result', 'protocol_hash', 'verifier_ref'),
    'cache_entries': ('cache_key', 'layer', 'scope', 'query_id', 'media_hash', 'allowed_cutoff', 'input_hash',
                      'model_hash', 'prompt_hash', 'observation_spec_hash', 'target_signature',
                      'extraction_scope_hash', 'result_ids', 'source_revisions', 'dependency_ids', 'creation_call_ids', 'status'),
}
REF_LISTS = {'scene_ids', 'window_ids', 'mention_ids', 'evidence_ids', 'counterevidence_ids',
             'claim_ids', 'all_window_ids', 'occurrence_ids', 'confirmed_occurrence_ids',
             'candidate_link_ids', 'entity_candidate_ids', 'prerequisite_ids', 'anchor_occurrence_ids',
             'target_requirement_ids', 'dependency_ids', 'result_ids', 'cannot_link_refs',
             'input_evidence_ids', 'binding_ids', 'requirement_ids', 'hypothesis_ids',
             'candidate_ids', 'basis_refs', 'target_binding_ids', 'new_object_ids', 'coverage_ids',
             'build_record_ids', 'cache_hit_ids', 'supported_claim_ids', 'refuted_claim_ids', 'proposed_patch_ids',
             'checked_requirement_ids', 'remaining_gap_refs', 'affected_ids'}
REF_FIELDS = {'scene_id', 'summary_id', 'prev_scene_id', 'next_scene_id', 'occurrence_id',
              'entity_id', 'mention_id', 'candidate_id', 'branch_id', 'parent_branch_id',
              'requirement_id', 'hypothesis_id', 'task_id', 'verifier_ref', 'stop_decision_id'}


def obj(id_, **fields):
    return dict(id=id_, schema_version=2, revision=0, **fields)


def valid_interval(interval, cutoff):
    return (isinstance(interval, (list, tuple)) and len(interval) == 2
            and all(isinstance(x, (int, float)) and math.isfinite(x) for x in interval)
            and 0 <= interval[0] < interval[1] <= cutoff)


def refs(value):
    result = []
    if isinstance(value, dict):
        for key, val in value.items():
            if key in REF_LISTS:
                result.extend(val)
            elif key in REF_FIELDS and val is not None:
                result.append(val)
            elif key == 'support_sets':
                result.extend(r for group in val for r in group)
            elif key == 'source_revisions':
                result.extend(val)
            elif isinstance(val, (dict, list)):
                result.extend(refs(val))
    elif isinstance(value, list):
        for item in value:
            result.extend(refs(item))
    return result


class Memory:
    def __init__(self, manifest, tables=None):
        self.manifest = deepcopy(manifest)
        self.tables = {t: {} for t in TABLES}
        if tables:
            for t, rows in tables.items():
                self.tables[t] = {r['id']: deepcopy(r) for r in rows}
        self.counters = {}

    def new_id(self, namespace, kind):
        key = f'{namespace}:{kind}'
        i = self.counters.get(key, 0) + 1
        while self.get(f'{key}{i:06d}', None) is not None:
            i += 1
        self.counters[key] = i
        return f'{key}{i:06d}'

    def get(self, ref, default=...):
        for table in self.tables.values():
            if ref in table:
                return table[ref]
        if ref == self.manifest.get('id'):
            return self.manifest
        for f in self.manifest.get('frames', []):
            if f['frame_id'] == ref:
                return f
        if default is ...:
            raise ValueError(f'Unresolved Ref: {ref}')
        return default

    def put(self, table, record):
        if table not in TABLES:
            raise ValueError(f'Unknown table: {table}')
        old = self.get(record['id'], None)
        if old is not None and record['revision'] != old['revision'] + 1:
            raise ValueError(f'Revision conflict: {record["id"]}')
        self.tables[table][record['id']] = deepcopy(record)
        # A proof of old boxes/pixels must not silently authorize a new source version.
        for kind in ('derived_claims', 'identity_links', 'claims', 'cannot_links'):
            for dependent in list(self.tables[kind].values()):
                expected = dependent.get('source_revisions', {}).get(record['id'])
                if (dependent['id'] != record['id'] and expected is not None and expected != record['revision']
                        and dependent.get('status') in VALID_STATUSES):
                    self.revoke(dependent['id'])
        return record

    def update(self, table, ref, **changes):
        old = self.tables[table][ref]
        return self.put(table, dict(old, revision=old['revision']+1, **changes))

    def is_valid(self, ref, seen=None):
        record = self.get(ref, None)
        if record is None:
            return False
        if any(self.get(r, {}).get('revision') != revision
               for r, revision in record.get('source_revisions', {}).items()):
            return False
        if ref in self.tables['evidence']:
            return bool(record['frame_inputs'] or record['text_spans'])
        if ref in self.tables['occurrences']:
            return bool(record['boxes']) and all(self.is_valid(e) for e in record['evidence_ids'])
        if ref in self.tables['entities']:
            return record.get('status', 'active') == 'active' and bool(record['confirmed_occurrence_ids'])
        if ref in self.tables['coverage']:
            return record['level'] == 'protocol_checked' and record['visibility'] == 'clear'
        if record.get('status') not in VALID_STATUSES:
            return False
        seen = set() if seen is None else seen
        if ref in seen:
            return False
        groups = record.get('support_sets')
        if groups is not None:
            return any(g and all(self.is_valid(r, seen | {ref}) for r in g) for g in groups)
        return bool(record.get('evidence_ids')) and all(self.is_valid(e, seen | {ref}) for e in record['evidence_ids'])

    @metered_cpu('dependency_revoke')
    def revoke(self, ref):
        record = self.get(ref)
        table = next(t for t in TABLES if ref in self.tables[t])
        status = 'revoked' if table in ('bindings', 'identity_links') else 'invalidated'
        self.update(table, ref, status=status)
        changed = [ref]
        repeat = True
        while repeat:
            repeat = False
            for t in ('claims', 'derived_claims', 'requirements', 'bindings', 'identity_links'):
                for r in list(self.tables[t].values()):
                    if r.get('status') in VALID_STATUSES and not self.is_valid(r['id']):
                        status = 'unresolved' if t == 'requirements' else ('revoked' if t in ('bindings', 'identity_links') else 'invalidated')
                        self.update(t, r['id'], status=status)
                        changed.append(r['id'])
                        repeat = True
        for c in list(self.tables['cache_entries'].values()):
            if any(r in changed for r in c['dependency_ids']):
                self.update('cache_entries', c['id'], status='stale')
        for c in list(self.tables['candidate_queue'].values()):
            if any(r in changed for r in c['binding_ids'] + c['basis_refs']):
                self.update('candidate_queue', c['id'], status='invalidated')
        return changed

    @metered_cpu('object_validation')
    def validate(self):
        cutoff = self.manifest['cutoff']
        frame_map = {f['frame_id']: f['pts'] for f in self.manifest['frames']}
        missing_frames = {f['frame_id'] for f in self.manifest['frames'] if f.get('missing', False)}
        for t, records in self.tables.items():
            for r in records.values():
                if r.get('schema_version') != 2 or not isinstance(r.get('revision'), int) or r['revision'] < 0:
                    raise ValueError(f'Invalid Common fields: {r}')
                if set(REQUIRED.get(t, ())) - set(r):
                    raise ValueError(f'Missing {t} fields: {set(REQUIRED[t])-set(r)}')
                if t in ENUMS and r['status'] not in ENUMS[t]:
                    raise ValueError(f'Invalid {t} status')
                for field in ('interval', 'scope', 'allowed_interval'):
                    if field in r and not (field == 'scope' and t == 'cache_entries') and not valid_interval(r[field], cutoff):
                        raise ValueError(f'Invalid {field}: {r["id"]}')
                for ref in refs(r):
                    self.get(ref)
                if t == 'evidence':
                    if not r['frame_inputs'] and not r['text_spans']:
                        raise ValueError('Evidence must contain real inputs')
                    for f in r['frame_inputs']:
                        if f['frame_id'] in missing_frames:
                            raise ValueError('Missing or imputed frames cannot be observation evidence')
                        if f['frame_id'] not in frame_map or abs(frame_map[f['frame_id']]-f['pts']) > 1e-6:
                            raise ValueError('Unknown frame or fabricated PTS')
                        if not r['interval'][0] <= f['pts'] < r['interval'][1] or f['width'] <= 0 or f['height'] <= 0:
                            raise ValueError('Frame is outside Evidence or has invalid resolution')
                        if f['crop'] is not None and not bbox_valid(f['crop']):
                            raise ValueError('Invalid Evidence crop')
                    for span in r['text_spans']:
                        if not valid_interval([span['start'], span['end']], cutoff):
                            raise ValueError('Subtitle exceeds legal prefix')
                if t == 'occurrences':
                    for b in r['boxes']:
                        if not bbox_valid(b['bbox']) or b['frame_id'] not in frame_map or frame_map[b['frame_id']] != b['pts']:
                            raise ValueError('Invalid tracked box')
                    if not all(valid_interval(i, cutoff) for i in r['visible_intervals']):
                        raise ValueError('Invalid visible interval')
                if t == 'claims':
                    self._validate_claim(r)
                if t == 'identity_links' and r['status'] == 'confirmed':
                    v = r['verification']
                    if not v or v['result'] != 'supported' or not v['distinguishing_facts'] or v['unresolved_conflicts'] or not r['evidence_ids']:
                        raise ValueError('Identity confirmation lacks distinguishing evidence')
                    if not any(f.startswith('anchor_registration:') for f in v['distinguishing_facts']):
                        if not set(v.get('basis_types', [])) & {'continuous_motion', 'unique_marker', 'multiple_details_with_exclusion'}:
                            raise ValueError('Identity has no qualifying discrimination basis')
                if t == 'requirements' and r['quantifier'] not in ('exists', 'last', 'all', 'count', 'negative', 'global'):
                    raise ValueError('Invalid quantifier')
                if t == 'coverage' and r['level'] == 'protocol_checked':
                    if (not r['actual_pts'] or not r['evidence_ids'] or r['visibility'] != 'clear'
                            or r['result'] == 'unknown' or r['verifier_ref'] is None):
                        raise ValueError('Unverified sampled data cannot be protocol_checked')
                    actual = {f['pts'] for e in r['evidence_ids'] for f in self.get(e)['frame_inputs']}
                    if not set(r['actual_pts']) <= actual:
                        raise ValueError('Fabricated coverage PTS')
        links = {}
        for r in self.tables['identity_links'].values():
            if r['status'] == 'confirmed':
                if self.get(r['entity_id']).get('status', 'active') != 'active':
                    raise ValueError('Confirmed identity refers to a retired entity')
                if r['occurrence_id'] in links:
                    raise ValueError('Two confirmed primary identities')
                links[r['occurrence_id']] = r['entity_id']
        visiting, done = set(), set()
        def visit(ref):
            if ref in visiting:
                raise ValueError('Dependency cycle')
            if ref in done:
                return
            visiting.add(ref)
            r = self.get(ref)
            edges = [x for g in r.get('support_sets', []) for x in g] + r.get('prerequisite_ids', [])
            for edge in edges:
                visit(edge)
            visiting.remove(ref)
            done.add(ref)
        for table in self.tables.values():
            for ref in table:
                visit(ref)
        for r in self.tables['derived_claims'].values():
            if r['status'] == 'supported' and not self.is_valid(r['id']):
                raise ValueError('Supported derived fact has no valid support')
        if self.tables['scenes']:
            scenes = sorted(self.tables['scenes'].values(), key=lambda r: r['interval'][0])
            if scenes[0]['interval'][0] != 0 or scenes[-1]['interval'][1] != cutoff:
                raise ValueError('Scene directory does not cover the prefix')
            for a, b in zip(scenes, scenes[1:]):
                if a['interval'][1] != b['interval'][0] or a['end_frame_exclusive'] != b['start_frame']:
                    raise ValueError('Scene gap or overlap')
            if scenes[0]['start_frame'] != 0 or scenes[-1]['end_frame_exclusive'] != len(frame_map):
                raise ValueError('Original frames not fully partitioned')

    def _validate_claim(self, r):
        if r['predicate'] not in PREDICATE_ROLES:
            raise ValueError('Unknown predicate')
        if r['status'] != 'supported':
            return
        if not r['support_sets'] or not self.is_valid(r['id']):
            raise ValueError('Supported claim lacks valid provenance')
        if not r['evidence_ids'] or set(PREDICATE_ROLES[r['predicate']]) - set(r['roles']):
            raise ValueError('Missing source or required role')
        for role in PREDICATE_ROLES[r['predicate']]:
            if role != 'content' and r['roles'][role] not in self.tables['occurrences']:
                raise ValueError('Claim role is not a localized Occurrence')
        if any(role != 'content' and ref not in self.tables['occurrences'] for role, ref in r['roles'].items()):
            raise ValueError('Claim contains an unlocalized entity role')
        v = r['verification']
        if not v or v['result'] != 'supported' or v['unresolved_conflicts']:
            raise ValueError('Claim has no successful semantic verification')
        evidence = [self.get(e) for e in r['evidence_ids']]
        actual_ids = {f['frame_id'] for e in evidence for f in e['frame_inputs']}
        for role in PREDICATE_ROLES[r['predicate']]:
            if role != 'content' and not any(b['frame_id'] in actual_ids and r['interval'][0] <= b['pts'] < r['interval'][1]
                                           for b in self.get(r['roles'][role])['boxes']):
                raise ValueError('Claim role was not localized in its actual inputs')
        if r['source_type'] == 'visual' and not any(e['frame_inputs'] for e in evidence):
            raise ValueError('Visual fact has only text sources')
        if r['source_type'] == 'visual':
            usable = {f['frame_id'] for e in evidence for f in e['frame_inputs'] if f.get('visual_usable', True)}
            if not usable or not set(r.get('source_frame_ids', [])) <= usable:
                raise ValueError('Visual fact cites unusable pixels')
            if r.get('fact_type') == 'inference':
                raise ValueError('Visual evidence cannot directly support an inferred purpose')
        if r['predicate'] in DYNAMIC or r.get('fact_type') == 'action':
            pts = {f['pts'] for e in evidence for f in e['frame_inputs']
                   if r['interval'][0] <= f['pts'] < r['interval'][1]
                   and (not r.get('source_frame_ids') or f['frame_id'] in r['source_frame_ids'])}
            if len(pts) < 3 or not v.get('process_visible'):
                raise ValueError('Dynamic action lacks observed process')
        if r['observed_at'] is not None and not r['interval'][0] <= r['observed_at'] < r['interval'][1]:
            raise ValueError('Point state lies outside its source interval')

    @contextmanager
    def transaction(self):
        old = deepcopy(self.tables)
        old_counters = deepcopy(self.counters)
        try:
            yield self
            self.validate()
        except BaseException:
            self.tables = old
            self.counters = old_counters
            raise


def bbox_valid(box):
    return (len(box) == 4 and all(isinstance(x, (float, int)) and math.isfinite(x) for x in box)
            and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)


class Store:
    """SQLite is the authoritative checkpoint; JSONL files are reviewable exports."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / '.writer.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise ValueError(f'Another writer owns {self.directory}') from exc
        self.db = sqlite3.connect(self.directory / 'state.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, kind TEXT, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, body TEXT)')
        self._serialized_objects = {id_: (kind, body) for id_, kind, body in self.db.execute('SELECT id,kind,body FROM objects')}
        self._export_initialized = False
        self._manifest_json = None
        self._support_json = None

    @metered_cpu('checkpoint_write')
    def checkpoint(self, memory, metadata, final=False):
        memory.validate()
        current = {r['id']: (t, json.dumps(r, ensure_ascii=False))
                   for t, table in memory.tables.items() for r in table.values()}
        changed = [(id_, kind, body) for id_, (kind, body) in current.items()
                   if self._serialized_objects.get(id_) != (kind, body)]
        deleted = set(self._serialized_objects)-set(current)
        changed_tables = {kind for _, kind, _ in changed}
        changed_tables.update(self._serialized_objects[id_][0] for id_ in deleted)
        with self.db:
            self.db.executemany('DELETE FROM objects WHERE id=?', [(id_,) for id_ in deleted])
            self.db.executemany('INSERT OR REPLACE INTO objects VALUES (?,?,?)', changed)
            self.db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                [(k, json.dumps(v, ensure_ascii=False)) for k, v in dict(metadata, manifest=memory.manifest).items()])
        self._serialized_objects = current
        self.export(memory, None if final or not self._export_initialized else changed_tables)
        self._export_initialized = True

    @metered_cpu('checkpoint_read')
    def load(self):
        meta = {k: json.loads(v) for k, v in self.db.execute('SELECT key,body FROM metadata')}
        rows = {}
        for t, body in self.db.execute('SELECT kind,body FROM objects ORDER BY id'):
            rows.setdefault(t, []).append(json.loads(body))
        memory = Memory(meta['manifest'], rows)
        memory.validate()
        return memory, meta

    def export(self, memory, changed_tables=None):
        manifest_json = json.dumps(memory.manifest, ensure_ascii=False)
        if manifest_json != self._manifest_json:
            write_json(self.directory / 'manifest.json', memory.manifest)
            self._manifest_json = manifest_json
        for t, table in memory.tables.items():
            if changed_tables is None or t in changed_tables:
                write_jsonl(self.directory / f'{t}.jsonl', sorted(table.values(), key=lambda r: r['id']))
        support_rows = [{'id': r['id'], 'support_sets': r['support_sets']}
                        for table in memory.tables.values() for r in table.values() if 'support_sets' in r]
        support_json = json.dumps(support_rows, ensure_ascii=False)
        if support_json != self._support_json:
            write_jsonl(self.directory / 'support_sets.jsonl', support_rows)
            self._support_json = support_json

    def close(self):
        self.db.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def finalize_accounting(self, memory, metadata, tables=()):
        """Write settled counters after the timed checkpoint; no new semantic edits here."""
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                [(k, json.dumps(v, ensure_ascii=False)) for k, v in metadata.items()])
            for table in tables:
                self.db.executemany('UPDATE objects SET body=? WHERE id=?',
                    [(json.dumps(r, ensure_ascii=False), r['id']) for r in memory.tables[table].values()])
                write_jsonl(self.directory / f'{table}.jsonl', list(memory.tables[table].values()))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temp.replace(path)


def write_jsonl(path, rows):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(''.join(json.dumps(r, ensure_ascii=False, allow_nan=False)+'\n' for r in rows))
    temp.replace(path)


def memory_hash(memory):
    return digest({'manifest': memory.manifest,
                   'tables': {t: sorted(rows.values(), key=lambda r: r['id']) for t, rows in memory.tables.items()}})
