"""Evidence-qualified B3 reassignment, merge and split transactions."""
from copy import deepcopy

from .config import digest
from .state import obj
from .tracking import compatible
from .accounting import metered_cpu

BASES = {'continuous_motion', 'unique_marker', 'multiple_details_with_exclusion'}


def primary_links(memory, entity_id=None):
    return [l for l in memory.tables['identity_links'].values() if l['status'] == 'confirmed'
            and memory.is_valid(l['id']) and (entity_id is None or l['entity_id'] == entity_id)]


def identity_frames(memory, left, right, per_occurrence=4):
    """Use actual representative context/crops from each occurrence, never invented timestamps."""
    frames = []
    for occurrence in (left, right):
        candidates = [f for eid in occurrence['evidence_ids'] for f in memory.get(eid)['frame_inputs']
                      if f['frame_id'] in {b['frame_id'] for b in occurrence['boxes']}]
        candidates = sorted({(f['frame_id'], f.get('path')): f for f in candidates}.values(),
                            key=lambda f: (f['pts'], f['crop'] is not None))
        crops = occurrence.get('crop_inputs', [])[-max(1, per_occurrence//2):]
        context_count = max(1, per_occurrence-len(crops))
        if candidates:
            indices = sorted(set(round(i*(len(candidates)-1)/max(1, context_count-1))
                                 for i in range(min(context_count, len(candidates)))))
            frames.extend(candidates[i] for i in indices)
        frames.extend(crops)
    return list({(f['frame_id'], f.get('path')): f for f in frames}.values())


def store_identity_proof(memory, namespace, left, right, report, cfg, frames):
    from .identity import verification
    from .identity import co_visible
    source_ids = set(report.get('source_frame_ids', []))
    if (report.get('result') not in ('same', 'different') or not report.get('distinguishing_facts')
            or report.get('unresolved_conflicts') or not set(report.get('basis_types', [])) & BASES
            or not source_ids or not source_ids <= {f['frame_id'] for f in frames}):
        raise ValueError('Identity proof lacks distinguishing actual inputs')
    for occurrence in (left, right):
        if not source_ids & {b['frame_id'] for b in occurrence['boxes']}:
            raise ValueError('Identity proof must cite both occurrences')
    if report['result'] == 'same' and co_visible(left, right):
        raise ValueError('Simultaneously separate instances cannot be merged')
    evidence_ids = sorted(set(left['evidence_ids']+right['evidence_ids']))
    signed = dict(report, result='supported')
    proof = obj(memory.new_id(namespace, 'identity_proof'), node_type='query_fact',
                content={'occurrence_ids': [left['id'], right['id']], 'identity_result': report['result'],
                         'source_frame_ids': sorted(source_ids),
                         'verification': verification(signed, evidence_ids, cfg, 'verify_identity')},
                evidence_ids=evidence_ids, support_sets=[evidence_ids], status='supported')
    proof['frame_inputs'] = deepcopy(frames)
    proof['source_revisions'] = {r: memory.get(r)['revision'] for r in [left['id'], right['id'], *evidence_ids]}
    memory.put('derived_claims', proof)
    return proof


def prepare_identity_patch(memory, namespace, operation, occurrence_ids, target_entity_id, proof_ids,
                           source_entity_id=None, summary_corrections=None):
    """Prepare a concrete proposal without changing links or entity membership."""
    if operation not in ('reassign', 'merge', 'split'):
        raise ValueError('Unknown identity patch operation')
    all_links = primary_links(memory)
    moved = [l for l in all_links if l['occurrence_id'] in set(occurrence_ids)]
    if len(moved) != len(set(occurrence_ids)) or not moved:
        raise ValueError('Every moved occurrence requires one confirmed primary link')
    source_entities = {l['entity_id'] for l in moved}
    if operation in ('merge', 'split') and source_entities != {source_entity_id}:
        raise ValueError('Merge/split must name the source entity')
    source_members = primary_links(memory, source_entity_id) if source_entity_id else []
    if operation == 'merge' and set(occurrence_ids) != {l['occurrence_id'] for l in source_members}:
        raise ValueError('Merge must move every member of the source entity')
    if operation == 'split' and not set(occurrence_ids) < {l['occurrence_id'] for l in source_members}:
        raise ValueError('Split must leave a nonempty source group')
    created = []
    if operation == 'split':
        if memory.get(target_entity_id, None) is not None:
            raise ValueError('Split target must be a fresh entity ID')
        template = memory.get(source_entity_id)
        created = [obj(target_entity_id, category=template['category'], aliases=deepcopy(template['aliases']),
                       descriptors=[], confirmed_occurrence_ids=[], candidate_link_ids=[], prototype_refs=[], status='active')]
        target_members = []
    else:
        if not memory.is_valid(target_entity_id) or target_entity_id in source_entities:
            raise ValueError('Target must be another active confirmed entity')
        target_members = primary_links(memory, target_entity_id)
    touched = {l['id'] for l in moved+source_members+target_members} | source_entities
    touched.update(l['occurrence_id'] for l in moved+source_members+target_members)
    if operation != 'split':
        touched.add(target_entity_id)
    evidence_ids = sorted({e for p in proof_ids for e in memory.get(p).get('evidence_ids', [])})
    touched.update(proof_ids+evidence_ids)
    for correction in summary_corrections or []:
        touched.add(correction['summary_id'])
        touched.update(correction.get('claim_ids', [])+correction['evidence_ids'])
    patch = obj(memory.new_id(namespace, 'patch'), operation=operation,
                occurrence_ids=sorted(set(occurrence_ids)), source_entity_id=source_entity_id,
                target_entity_id=target_entity_id, proof_ids=list(proof_ids), new_entities=created,
                expected_revisions={ref: memory.get(ref)['revision'] for ref in sorted(touched)},
                changes=[{'object_id': l['id'], 'field': 'entity_id', 'old': l['entity_id'],
                          'proposed': target_entity_id} for l in moved],
                evidence_ids=evidence_ids, affected_ids=sorted(touched),
                summary_corrections=summary_corrections or [], status='proposed', reason=operation)
    return patch


def _proof(memory, patch, left_id, right_id, result):
    from .cache import safe_source
    pair = {left_id, right_id}
    for ref in patch['proof_ids']:
        proof = memory.get(ref)
        from .video import file_hash
        from pathlib import Path
        if any(f.get('content_hash') and (not Path(f['path']).is_file() or file_hash(f['path']) != f['content_hash'])
               for f in proof.get('frame_inputs', [])):
            continue
        content = proof.get('content', {})
        verify = content.get('verification', {})
        if (memory.is_valid(ref) and safe_source(memory, ref, [0., memory.manifest['cutoff']])
                and set(content.get('occurrence_ids', [])) == pair
                and content.get('identity_result') == result and verify.get('result') == 'supported'
                and set(verify.get('basis_types', [])) & BASES and verify.get('distinguishing_facts')
                and not verify.get('unresolved_conflicts')):
            source_ids = set(content.get('source_frame_ids', []))
            actual = {f['frame_id'] for e in proof['evidence_ids'] for f in memory.get(e)['frame_inputs']}
            if not source_ids or not source_ids <= actual:
                continue
            if all(source_ids & {b['frame_id'] for b in memory.get(o)['boxes']} for o in pair):
                return proof
    raise ValueError(f'Missing independently verified {result} proof for {left_id}, {right_id}')


@metered_cpu('identity_patch_commit')
def commit_patch(memory, patch, method):
    from .identity import co_visible
    if method != 'B3':
        raise ValueError('Identity cluster transactions are exclusive to B3')
    with memory.transaction():
        for ref, revision in patch['expected_revisions'].items():
            if memory.get(ref)['revision'] != revision:
                raise ValueError('Patch revision conflict')
        saved_counters = deepcopy(memory.counters)
        expected = prepare_identity_patch(memory, 'validation', patch['operation'], patch['occurrence_ids'],
                                          patch['target_entity_id'], patch['proof_ids'], patch['source_entity_id'],
                                          patch.get('summary_corrections'))
        # Building this validation proposal must not consume visible ID allocation state.
        memory.counters = saved_counters
        if patch['changes'] != expected['changes'] or patch['new_entities'] != expected['new_entities']:
            raise ValueError('Patch changes do not match the declared operation')
        if set(expected['expected_revisions'])-set(patch['expected_revisions']):
            raise ValueError('Patch omits a touched object revision')
        moved = patch['occurrence_ids']
        retained = [l['occurrence_id'] for l in primary_links(memory, patch['source_entity_id'])
                    if l['occurrence_id'] not in moved] if patch['source_entity_id'] else []
        target = [l['occurrence_id'] for l in primary_links(memory, patch['target_entity_id'])]
        anchor = moved[0] if patch['operation'] == 'split' else target[0]
        for oid in moved:
            if not compatible(memory.get(oid)['category'], memory.get(anchor)['category']):
                raise ValueError('Identity patch crosses incompatible categories')
            if oid != anchor:
                _proof(memory, patch, oid, anchor, 'same')
            for other in (retained if patch['operation'] == 'split' else target):
                if patch['operation'] == 'split':
                    _proof(memory, patch, oid, other, 'different')
                elif co_visible(memory.get(oid), memory.get(other)):
                    raise ValueError('Patch merges co-visible separate occurrences')
                elif any({oid, other} <= set(c['occurrence_ids']) and memory.is_valid(c['id'])
                         for c in memory.tables['cannot_links'].values()):
                    raise ValueError('Patch violates an existing cannot-link')
        for entity in patch['new_entities']:
            memory.put('entities', entity)
        if patch['operation'] == 'split':
            for oid in moved:
                for other in retained:
                    proof = _proof(memory, patch, oid, other, 'different')
                    if not any({oid, other} == set(c['occurrence_ids']) for c in memory.tables['cannot_links'].values()):
                        memory.put('cannot_links', obj(memory.new_id(patch['id'], 'cannot_link'),
                            occurrence_ids=[oid, other], evidence_ids=proof['evidence_ids'], status='supported',
                            support_sets=[proof['evidence_ids']], source_revisions=proof['source_revisions'],
                            verification=proof['content']['verification']))
        revoked = []
        for change in patch['changes']:
            link = memory.get(change['object_id'])
            if link[change['field']] != change['old']:
                raise ValueError('Patch old value mismatch')
            revoked.extend(memory.revoke(link['id']))
            if link['occurrence_id'] == anchor:
                proof = _proof(memory, patch, anchor, retained[0], 'different')
            else:
                proof = _proof(memory, patch, link['occurrence_id'], anchor, 'same')
            verify = deepcopy(proof['content']['verification'])
            if patch['operation'] == 'split' and link['occurrence_id'] == anchor:
                verify['distinguishing_facts'] = ['anchor_registration: split instance']+verify['distinguishing_facts']
            memory.update('identity_links', link['id'], entity_id=change['proposed'], status='confirmed',
                          evidence_ids=proof['evidence_ids'], verification=verify, patch_id=patch['id'],
                          support_sets=[proof['evidence_ids']], source_revisions=proof['source_revisions'])
        affected_entities = {c['old'] for c in patch['changes']} | {patch['target_entity_id']}
        for eid in sorted(affected_entities):
            members = [l['occurrence_id'] for l in primary_links(memory, eid)]
            occurrences = [memory.get(o) for o in members]
            entity = memory.get(eid)
            memory.update('entities', eid, confirmed_occurrence_ids=members,
                          descriptors=list(dict.fromkeys(o['description'] for o in occurrences)),
                          prototype_refs=list(dict.fromkeys(c for o in occurrences for c in o['crop_refs'])),
                          status='active' if members else 'retired')
        for correction in patch.get('summary_corrections', []):
            if (correction['summary_id'] not in memory.tables['summaries']
                    or not correction['evidence_ids'] or not set(correction['evidence_ids']) <= set(patch['evidence_ids'])):
                raise ValueError('Summary correction must cite patch evidence')
            memory.put('summary_corrections', obj(memory.new_id(patch['id'], 'correction'),
                **dict(correction, source_revision=memory.get(correction['summary_id'])['revision'],
                       claim_ids=correction.get('claim_ids', [])),
                support_sets=[patch['proof_ids']], status='supported', patch_id=patch['id']))
        memory.put('patches', dict(patch, status='committed', revoked_ids=sorted(set(revoked)),
                                 index_hash=digest([(l['id'], l['revision'], l['entity_id']) for l in primary_links(memory)])))
    return memory.get(patch['id'])
