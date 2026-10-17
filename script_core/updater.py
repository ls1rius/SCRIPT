"""Sourced query corrections and independently verified B3 cluster transactions."""
from .identity import commit_identity_patch, verification
from .patches import identity_frames, primary_links, prepare_identity_patch, store_identity_proof
from .state import obj


def verify_pair(memory, left, right, meter, cfg, namespace, task_id=None):
    pair = {left['id'], right['id']}
    for proof in memory.tables['derived_claims'].values():
        if (proof.get('content', {}).get('identity_result') in ('same', 'different')
                and set(proof['content'].get('occurrence_ids', [])) == pair and memory.is_valid(proof['id'])):
            return proof
    frames = identity_frames(memory, left, right)
    report, _ = meter.json('verify_identity', {'frames': frames, 'left': left, 'right': right,
                                              'occurrences': [left, right]},
                          ('result', 'distinguishing_facts', 'unresolved_conflicts', 'source_frame_ids', 'basis_types'), task_id)
    return store_identity_proof(memory, namespace, left, right, report, cfg, frames)


def apply_identity_suggestions(memory, suggestions, meter, cfg, namespace, task_id=None):
    patch_ids = []
    for suggestion in suggestions:
        oid, eid = suggestion.get('occurrence_id'), suggestion.get('entity_id')
        if oid not in memory.tables['occurrences'] or eid not in memory.tables['entities']:
            continue
        claim_ids = suggestion.get('claim_ids', [])
        if not claim_ids or not all(c in memory.tables['claims'] and memory.is_valid(c) for c in claim_ids):
            continue
        old_links = [l for l in primary_links(memory) if l['occurrence_id'] == oid]
        target_links = primary_links(memory, eid)
        if len(old_links) != 1 or not target_links or old_links[0]['entity_id'] == eid:
            continue
        try:
            proof = verify_pair(memory, memory.get(oid), memory.get(target_links[-1]['occurrence_id']),
                                meter, cfg, namespace, task_id)
            if proof['content']['identity_result'] != 'same':
                continue
            old = old_links[0]
            if cfg['method'] == 'B3':
                patch = prepare_identity_patch(memory, namespace, 'reassign', [oid], eid, [proof['id']])
                commit_identity_patch(memory, patch, 'B3')
                patch_ids.append(patch['id'])
            else:
                use = obj(memory.new_id(namespace, 'identity_use'), node_type='query_fact',
                          content={'occurrence_id': oid, 'entity_id': eid,
                                   'verification': proof['content']['verification']},
                          support_sets=[[proof['id']]], status='supported')
                memory.put('derived_claims', use)
                memory.revoke(old['id'])
                memory.update('identity_links', old['id'], status='conflicted')
        except ValueError:
            # Ambiguous/invalid proposals do not edit identity clusters; paid observations remain.
            continue
    return patch_ids


def apply_cluster_suggestions(memory, suggestions, meter, cfg, namespace, task_id=None):
    if cfg['method'] != 'B3':
        return []
    committed = []
    for suggestion in suggestions:
        operation = suggestion.get('operation')
        source = suggestion.get('source_entity_id')
        if operation not in ('merge', 'split') or not memory.is_valid(source):
            continue
        claims = suggestion.get('claim_ids', [])
        if not claims or not all(c in memory.tables['claims'] and memory.is_valid(c) for c in claims):
            continue
        source_links = primary_links(memory, source)
        moved = ([l['occurrence_id'] for l in source_links] if operation == 'merge'
                 else suggestion.get('occurrence_ids', []))
        target = suggestion.get('target_entity_id')
        if operation == 'split':
            target = memory.new_id(namespace, 'E')
        try:
            retained = [l['occurrence_id'] for l in source_links if l['occurrence_id'] not in moved]
            anchors = [l['occurrence_id'] for l in primary_links(memory, target)] if operation == 'merge' else moved[:1]
            if not anchors or (operation == 'split' and not retained):
                continue
            anchor = anchors[0]
            proofs = []
            for oid in moved:
                if oid != anchor:
                    proof = verify_pair(memory, memory.get(oid), memory.get(anchor), meter, cfg, namespace, task_id)
                    if proof['content']['identity_result'] != 'same':
                        raise ValueError('Merge group not confirmed same')
                    proofs.append(proof['id'])
                if operation == 'split':
                    for other in retained:
                        proof = verify_pair(memory, memory.get(oid), memory.get(other), meter, cfg, namespace, task_id)
                        if proof['content']['identity_result'] != 'different':
                            raise ValueError('Split groups not confirmed different')
                        proofs.append(proof['id'])
            patch = prepare_identity_patch(memory, namespace, operation, moved, target,
                                           sorted(set(proofs)), source)
            commit_identity_patch(memory, patch, 'B3')
            committed.append(patch['id'])
        except ValueError as exc:
            memory.put('patches', obj(memory.new_id(namespace, 'patch'), operation=operation,
                status='rejected', reason=str(exc), source_entity_id=source,
                occurrence_ids=[o for o in moved if o in memory.tables['occurrences']], evidence_ids=[], affected_ids=[]))
    return committed
