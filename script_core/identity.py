"""Confirmed known-only index and visual verification before cross-scene identity."""
from .coverage import checked_intervals, complement
from .prompts import prompt_hash
from .config import digest
from .state import obj
from .tracking import compatible, cosine, iou


def verification(report, evidence_ids, cfg, operation):
    origin = report.get('_replay_origin')
    return {'result': report['result'], 'rule_version': 'script-v2',
            'model_hash': origin['source_model_hash'] if origin else digest(cfg['models']),
            'prompt_hash': (origin['source_prompt_hash'] or 'unknown-recorded-source') if origin else prompt_hash(operation),
            'replay_origin': origin,
            'input_evidence_ids': evidence_ids, 'distinguishing_facts': report.get('distinguishing_facts', []),
            'unresolved_conflicts': report.get('unresolved_conflicts', []),
            'process_visible': report.get('process_visible', False), 'basis_types': report.get('basis_types', []),
            **({'whole_claim_supported': report['whole_claim_supported']} if 'whole_claim_supported' in report else {})}


def identity_index(memory, entity_id, scope, requirement_id, relation='within', at=None):
    links = [l for l in memory.tables['identity_links'].values()
             if l['entity_id'] == entity_id and l['status'] == 'confirmed' and memory.is_valid(l['id'])]
    def legal(link):
        o = memory.get(link['occurrence_id'])
        first, last = o['boxes'][0]['pts'], o['boxes'][-1]['pts']
        return (scope[0] <= first < scope[1] and
                (relation == 'within' or (relation == 'before' and last < at)
                 or (relation == 'after' and first > at)))
    links = sorted(filter(legal, links), key=lambda l: (
        memory.get(memory.get(l['occurrence_id'])['scene_id'])['interval'][0],
        memory.get(l['occurrence_id'])['boxes'][0]['pts'], l['occurrence_id']))
    return {'entity_id': entity_id, 'occurrence_ids': [l['occurrence_id'] for l in links],
            'link_ids': [l['id'] for l in links], 'query_scope': scope,
            'index_revision': sum(l['revision']+1 for l in memory.tables['identity_links'].values()),
            'known_only': True, 'unsearched_intervals': complement(scope, checked_intervals(memory, requirement_id))}


def co_visible(a, b):
    boxes = {x['frame_id']: x for x in a['boxes']}
    return any(x['frame_id'] in boxes and iou(x['bbox'], boxes[x['frame_id']]['bbox']) < .1 for x in b['boxes'])


class EntityResolver:
    def __init__(self, memory, meter, cfg, namespace):
        self.memory, self.meter, self.cfg, self.namespace = memory, meter, cfg, namespace

    def anchor(self, occurrence):
        memory = self.memory
        entity = obj(memory.new_id(self.namespace, 'E'), category=occurrence['category'], aliases=[],
                     descriptors=[occurrence['description']], confirmed_occurrence_ids=[occurrence['id']],
                     candidate_link_ids=[], prototype_refs=occurrence['crop_refs'])
        memory.put('entities', entity)
        report = {'result': 'supported', 'distinguishing_facts': ['anchor_registration: this ID names the observed instance'],
                  'unresolved_conflicts': []}
        link = self._link(occurrence, entity, 'confirmed', 1., occurrence['evidence_ids'], report)
        return link

    def _link(self, occurrence, entity, status, score, evidence_ids, report=None):
        link = obj(self.memory.new_id(self.namespace, 'identity'), occurrence_id=occurrence['id'],
                   entity_id=entity['id'], status=status, retrieval_score=score, evidence_ids=evidence_ids,
                   cannot_link_refs=[], verification=verification(report, evidence_ids, self.cfg, 'verify_identity') if report else None)
        self.memory.put('identity_links', link)
        return link

    def resolve(self, occurrence, task_id=None):
        existing = [l for l in self.memory.tables['identity_links'].values()
                    if l['occurrence_id'] == occurrence['id'] and l['status'] == 'confirmed' and self.memory.is_valid(l['id'])]
        if existing:
            return existing[0]
        candidates = []
        cfg = self.cfg['identity']
        for entity in self.memory.tables['entities'].values():
            if not compatible(entity['category'], occurrence['category']):
                continue
            members = [self.memory.get(l['occurrence_id']) for l in self.memory.tables['identity_links'].values()
                       if l['entity_id'] == entity['id'] and l['status'] == 'confirmed' and self.memory.is_valid(l['id'])]
            members = sorted(members, key=lambda o: o['boxes'][0]['pts'])[-cfg['entity_history_limit']:]
            if not members:
                continue
            visual = max(cosine(a, b) for a in occurrence['features'] for m in members for b in m['features'])
            if visual < cfg['candidate_min_visual_cosine']:
                continue
            out, _ = self.meter.call('embed', {'texts': [occurrence['description'], members[-1]['description']]}, task_id)
            textual = cosine(*out['vectors'])
            candidates.append((cfg['visual_weight']*visual+cfg['description_weight']*textual, entity, members))
        candidates.sort(key=lambda x: (-x[0], x[1]['id']))
        if not candidates:
            return self.anchor(occurrence)
        unknown = False
        for score, entity, members in candidates[:cfg['candidate_top_k']]:
            link = self._link(occurrence, entity, 'candidate', score, occurrence['evidence_ids'])
            member = members[-1]
            evidence_ids = list(dict.fromkeys(occurrence['evidence_ids']+member['evidence_ids']))
            from .patches import identity_frames, store_identity_proof
            frame_data = identity_frames(self.memory, member, occurrence)
            report, _ = self.meter.json('verify_identity', {'frames': frame_data, 'left': member,
                                                           'right': occurrence,
                                                           'occurrences': [member, occurrence],
                                                           'co_visible_spatially_separate': co_visible(member, occurrence)},
                                        ('result', 'distinguishing_facts', 'unresolved_conflicts', 'source_frame_ids'), task_id)
            source_ids = {f['frame_id'] for f in frame_data}
            if not set(report['source_frame_ids']) <= source_ids or not report['source_frame_ids']:
                report['result'] = 'unknown'
            qualified_basis = set(report.get('basis_types', [])) & {'continuous_motion', 'unique_marker', 'multiple_details_with_exclusion'}
            proof = None
            if report['result'] in ('same', 'different') and qualified_basis:
                try:
                    proof = store_identity_proof(self.memory, self.namespace, member, occurrence, report, self.cfg, frame_data)
                except ValueError:
                    report['result'] = 'unknown'
            if report['result'] == 'same' and qualified_basis and report['distinguishing_facts'] and not report['unresolved_conflicts']:
                report['result'] = 'supported'
                confirmed = self.memory.update('identity_links', link['id'], status='confirmed',
                                                evidence_ids=evidence_ids,
                                                verification=verification(report, evidence_ids, self.cfg, 'verify_identity'),
                                                support_sets=[proof['evidence_ids']],
                                                source_revisions=proof['source_revisions'])
                self.memory.update('entities', entity['id'], confirmed_occurrence_ids=entity['confirmed_occurrence_ids']+[occurrence['id']])
                return confirmed
            if report['result'] == 'different' and proof:
                report['result'] = 'refuted'
                cannot = obj(self.memory.new_id(self.namespace, 'cannot_link'),
                             occurrence_ids=[member['id'], occurrence['id']], evidence_ids=evidence_ids,
                             status='supported', support_sets=[proof['evidence_ids']],
                             source_revisions=proof['source_revisions'],
                             verification=verification(report, evidence_ids, self.cfg, 'verify_identity'))
                self.memory.put('cannot_links', cannot)
                self.memory.update('identity_links', link['id'], status='revoked',
                                   evidence_ids=evidence_ids, cannot_link_refs=[cannot['id']],
                                   verification=verification(report, evidence_ids, self.cfg, 'verify_identity'))
            else:
                unknown = True
        # Excluding only top-k candidates does not exclude all known entities.
        if unknown or len(candidates) > cfg['candidate_top_k']:
            return None
        return self.anchor(occurrence)


def commit_identity_patch(memory, patch, method):
    """Only typed, independently verified proposals can revise committed clusters."""
    if method != 'B3':
        raise ValueError('Committed identity reassignment is exclusive to B3')
    if 'operation' not in patch:
        raise ValueError('Identity patch requires a typed operation and verified proof IDs')
    from .patches import commit_patch
    return commit_patch(memory, patch, method)
