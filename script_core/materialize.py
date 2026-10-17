"""Bounded local observations, target extraction, verification and cache reuse."""
from copy import deepcopy
from pathlib import Path

from .backend import DETECTION_TERMS
from .budget import ResourceExhausted, ToolFailure
from .build import evidence_record
from .cache import CacheManager, cache_key
from .config import digest
from .coverage import coverage_record, intersect, complement
from .identity import EntityResolver, verification
from .prompts import prompt_hash
from .state import DYNAMIC, PREDICATE_ROLES, obj
from .tracking import register_tracks
from .accounting import metered_cpu
from .query_policy import global_checkpoints, observation_focus
from .coverage import midpoint_observed, midpoint_attempted


class Materializer:
    def __init__(self, memory, meter, cfg, namespace, output):
        self.memory, self.meter, self.cfg, self.namespace = memory, meter, cfg, namespace
        self.output = Path(output)
        self.cache = CacheManager(memory, namespace, [0., memory.manifest['cutoff']], cfg)
        self.resolver = EntityResolver(memory, meter, cfg, namespace)

    def execute(self, task, inputs_to_check, interpret=True):
        memory, ledger, cfg = self.memory, self.meter.ledger, self.cfg
        initial_ids = {r for table in memory.tables.values() for r in table}
        initial_claims = {r for r in memory.tables['claims'] if memory.is_valid(r)}
        self.last_patch_ids = []
        start_cost, start_calls, start_hits = ledger.actual, len(ledger.calls), len(self.cache.hits)
        evidence_ids, build_ids, coverage_ids, unresolved, observation_gaps = [], [], [], [], []
        # Observation must leave time for interpreting the resulting facts.
        reserve = min(task['cost_cap']/2, self.meter.expected_cost('assess')) if interpret else 0.
        ledger.task_start, ledger.task_cap = ledger.actual, max(0., task['cost_cap']-reserve)
        failed = False
        try:
            for request in inputs_to_check[:task['max_new_observations']]:
                interval = request['interval']
                if not intersect(interval, task['allowed_interval']) == interval:
                    raise ValueError('Local observation exceeds task authorization')
                try:
                    added, record, covs = self.observe(task, request)
                    evidence_ids.extend(added)
                    build_ids.append(record['id'])
                    coverage_ids.extend(covs)
                    observation_gaps.extend({'interval': record['interval'], 'reason': gap}
                                            for gap in record.get('observation_gaps', []))
                except (ResourceExhausted, ToolFailure) as exc:
                    unresolved.append(str(exc))
                    failed = True
                    break
        finally:
            ledger.task_cap = None
        supported = [c['id'] for c in memory.tables['claims'].values()
                     if c['id'] not in initial_claims and memory.is_valid(c['id'])]
        refuted = [c['id'] for c in memory.tables['claims'].values()
                   if c['id'] not in initial_ids and c['status'] == 'refuted']
        updates, discriminations, leads = [], [], []
        # This semantic interpretation is separately paid, within the same local allowance.
        ledger.task_start, ledger.task_cap = start_cost, task['cost_cap']
        try:
            requirements = [memory.get(r) for r in task['requirement_ids']]
            # An overview cannot pass before its temporal checkpoints are read.
            # Interpret the accumulated facts once that gate can actually run.
            defer = (requirements and all(r['quantifier'] == 'global' and not r['roles'] for r in requirements)
                     and any(not any(e.get('online') and midpoint_attempted(e, point, cfg)
                                     for e in memory.tables['evidence'].values())
                             for r in requirements for point in global_checkpoints(r['scope'], cfg)))
            if interpret and requirements and not defer and any(memory.is_valid(c['id']) for c in memory.tables['claims'].values()):
                report, _ = self.meter.json('assess', {
                    'question': task.get('question', task['goal']), 'goal': task['goal'], 'options': task.get('options', []),
                    'requirements': [memory.get(r) for r in task['requirement_ids']],
                    'variables': task.get('variables', []),
                    'global_summary_view': getattr(self, 'global_summary_view', None),
                    'claims': [c for c in memory.tables['claims'].values() if memory.is_valid(c['id'])],
                    'bindings': [b for b in memory.tables['bindings'].values() if memory.is_valid(b['id'])],
                    'occurrences': list(memory.tables['occurrences'].values()),
                    'entities': list(memory.tables['entities'].values()),
                    'identity_links': list(memory.tables['identity_links'].values()),
                    'allowed_scene_ids': list(memory.tables['scenes']),
                    'allowed_binding_ids': list(memory.tables['bindings'])},
                    ('bindings', 'requirement_updates', 'candidate_discriminations', 'next_leads', 'unresolved'), task['id'])
                updates, discriminations, leads = self.integrate_assessment(task, report)
                unresolved.extend(report['unresolved'])
        except (ResourceExhausted, ToolFailure) as exc:
            failed = True
            unresolved.append(str(exc))
        finally:
            ledger.task_cap = None
        complete = all(memory.is_valid(r) for r in task['requirement_ids'])
        progress = supported or coverage_ids or any(u['result'] == 'supported' for u in updates)
        contradicted = any(d['result'] == 'different' for d in discriminations)
        status = ('contradicted' if contradicted else 'resolved' if complete else 'partial'
                  if progress else 'blocked' if failed else 'not_found')
        checked = [memory.get(b)['interval'] for b in build_ids if memory.get(b)['status'] == 'complete']
        result = obj(memory.new_id(self.namespace, 'local_result'), task_id=task['id'], task_status=status,
                     supported_claim_ids=supported, refuted_claim_ids=refuted,
                     requirement_updates=updates, candidate_discriminations=discriminations,
                     unresolved=[{'requirement_id': r, 'reason': '; '.join(map(str, unresolved)) or 'Missing support'}
                                 for r in task['requirement_ids'] if not memory.is_valid(r)],
                     next_leads=leads, evidence_ids=list(dict.fromkeys(evidence_ids)), coverage_ids=coverage_ids,
                     observation_gaps=observation_gaps,
                     new_object_ids=[r for table in memory.tables.values() for r in table if r not in initial_ids],
                     build_record_ids=build_ids, cache_hit_ids=self.cache.hits[start_hits:],
                     unchecked_intervals=complement(task['allowed_interval'], checked), proposed_patch_ids=self.last_patch_ids,
                     actual_cost=ledger.actual-start_cost, call_ids=[c['call_id'] for c in ledger.calls[start_calls:]])
        memory.put('local_results', result)
        return result

    @metered_cpu('observation_maintenance')
    def observe(self, task, request):
        memory, cfg = self.memory, self.cfg
        interval, scene_ids = request['interval'], task['scene_ids']
        spec = {'fps': request.get('fps', cfg['observation']['base_fps']),
                'longest_side': request.get('longest_side', cfg['observation']['longest_side']),
                'crop': request.get('crop')}
        start_cost, start_call = self.meter.ledger.actual, len(self.meter.ledger.calls)
        decoded, call = self.meter.call('decode', {'manifest': memory.manifest, 'interval': interval,
                                                  'output': str(self.output / 'frames'), **spec}, task['id'])
        frames = decoded['data']
        if spec['crop']:
            context, _ = self.meter.call('decode', {'manifest': memory.manifest, 'interval': interval,
                                                  'output': str(self.output / 'frames'), 'fps': spec['fps'],
                                                  'longest_side': cfg['observation']['longest_side']}, task['id'])
            frames += context['data']
        if not frames:
            raise ToolFailure('No legal frames in selected observation interval')
        from .video import load_subtitles
        subtitles = load_subtitles(memory.manifest, interval) if cfg['initialization']['use_provided_subtitles'] else []
        scope = task['extraction_scope']
        direct = (scope['predicates'] == ['other'] and scope['mode'] == 'goal'
                  and not any(memory.get(r)['roles'] or memory.get(r)['quantifier'] in ('negative', 'count', 'all')
                              for r in task['requirement_ids']))
        model_hash = digest(cfg['models'])
        focus = observation_focus(task)
        # A repeated frame package with a different distinction/goal is a new
        # extraction request. Pixel/detection caches remain reusable.
        key = digest({'inputs': cache_key(memory.manifest, frames, model_hash,
                      prompt_hash('observe' if direct else 'extract'), spec, task['target_signature'], scope, subtitles),
                      'goal': task['goal'], 'focus': focus})
        hit = self.cache.lookup(key, 'outputs', task['target_signature'], scope)
        if hit:
            record = memory.get(hit['payload']['build_record_id'])
            return record['evidence_ids'], record, hit['payload'].get('coverage_ids', [])
        record = obj(memory.new_id(self.namespace, 'build'), query_id=self.namespace,
                     scene_ids=scene_ids, interval=interval, target_signature=task['target_signature'],
                     extraction_scope=scope, observation_spec_hash=digest(spec), input_hash=key,
                     config_hash=digest(cfg), evidence_ids=[], result_ids=[], status='running',
                     unchecked_intervals=[interval], failure_reason=None, call_ids=[], actual_cost=0.)
        record['observation_focus_hash'] = digest(focus)
        record['observation_gaps'] = []
        memory.put('build_records', record)
        evidence = evidence_record(memory, self.namespace, scene_ids, interval, frames, subtitles,
                                   'Detailed observation inputs', call, cfg)
        evidence['online'] = True
        memory.put('evidence', evidence)
        memory.put('observations', obj(memory.new_id(self.namespace, 'obs'), evidence_ids=[evidence['id']],
                                       actual_pts=[f['pts'] for f in frames], specification=spec, input_hash=key))
        result_ids, coverage_ids = [], []
        try:
            # Keep every decoded input in provenance, but black pixels cannot support
            # actions or satisfy a visual checkpoint. This is a normal observation gap.
            frames = [f for f in frames if f.get('visual_usable', True)]
            if not frames:
                memory.update('build_records', record['id'], evidence_ids=[evidence['id']],
                              status='partial', unchecked_intervals=[interval], failure_reason='no_usable_visual_frames',
                              call_ids=[c['call_id'] for c in self.meter.ledger.calls[start_call:]],
                              actual_cost=self.meter.ledger.actual-start_cost)
                return [evidence['id']], memory.get(record['id']), []
            if direct:
                report, _ = self.meter.json('observe', {'frames': frames, 'subtitles': subtitles, 'interval': interval,
                                                       'goal': task['goal'], **focus,
                                                       'max_claims': 3}, ('claims', 'uncertainties'), task['id'])
                memory.update('build_records', record['id'], observation_gaps=report['uncertainties'])
                proposed_claims = report['claims']
                checks = {}
                if proposed_claims:
                    verified, _ = self.meter.json('verify_claims', {
                        'frames': frames, 'subtitles': subtitles,
                        'require_complete_claim': True,
                        'candidates': {str(i): candidate for i, candidate in enumerate(proposed_claims)}},
                        ('checks',), task['id'])
                    checks = verified['checks']
                    if verified.get('_replay_origin'):
                        checks = {key: dict(check, _replay_origin=verified['_replay_origin'])
                                  for key, check in checks.items()}
                for i, proposed in enumerate(proposed_claims):
                    claim = self.verify_and_add_claim(task, proposed, evidence, [], checks[str(i)], 'verify_claims')
                    if claim:
                        result_ids.append(claim['id'])
                memory.update('build_records', record['id'], evidence_ids=[evidence['id']], result_ids=result_ids,
                              status='complete', unchecked_intervals=[], failure_reason=None,
                              call_ids=[c['call_id'] for c in self.meter.ledger.calls[start_call:]],
                              actual_cost=self.meter.ledger.actual-start_cost)
                record = memory.get(record['id'])
                dependencies = [evidence['id']] + [r for r in result_ids if memory.is_valid(r)]
                self.cache.save(key, 'outputs', evidence['input_hash'], model_hash, prompt_hash('observe'), spec,
                                result_ids, dependencies, record['call_ids'], task['target_signature'], scope,
                                payload={'build_record_id': record['id'], 'coverage_ids': []})
                verified = [r for r in result_ids if memory.is_valid(r)]
                if verified:
                    self.cache.save(key, 'verified_facts', evidence['input_hash'], model_hash, prompt_hash('verify_claims'), spec,
                                    verified, [evidence['id'], *verified], record['call_ids'], task['target_signature'], scope)
                memory.validate()
                return [evidence['id']], record, []
            # Detection cache is target independent, while extraction caches are target dependent.
            det_key = cache_key(memory.manifest, frames, model_hash, digest(DETECTION_TERMS), spec)
            det_hit = self.cache.lookup(det_key, 'inputs')
            if det_hit:
                detections = det_hit['payload']['detections']
            else:
                detection, det_call = self.meter.call('detect', {'frames': frames, 'terms': DETECTION_TERMS,
                                                               'crop_directory': str(self.output / 'crops')}, task['id'])
                detections = detection['detections']
                if cfg['materialization']['supplemental_detection_rounds']:
                    supplement, _ = self.meter.json('supplement', {'frames': frames, 'existing_terms': DETECTION_TERMS,
                                                                 'max_terms': cfg['materialization']['supplemental_terms_per_window']},
                                                    ('terms', 'source_frame_ids'), task['id'])
                    if not set(supplement['source_frame_ids']) <= {f['frame_id'] for f in frames}:
                        raise ToolFailure('Supplemental detection references an unread frame')
                    terms = supplement['terms'][:cfg['materialization']['supplemental_terms_per_window']]
                    if terms:
                        extra, _ = self.meter.call('detect', {'frames': frames, 'terms': terms,
                                                            'crop_directory': str(self.output / 'crops')}, task['id'])
                        detections += extra['detections']
                self.cache.save(det_key, 'inputs', evidence['input_hash'], model_hash, digest(DETECTION_TERMS),
                                spec, [], [evidence['id']], [det_call['call_id']], payload={'detections': detections})
            tracked, _ = self.meter.call('track', {'detections': detections,
                                                  'cfg': cfg['tracking'] | {'same_class_nms_iou': cfg['materialization']['same_class_nms_iou']}}, task['id'])
            # Each track belongs to an original continuous scene; crossing scene identity is separate.
            occurrences = []
            for scene_id in scene_ids:
                scene = memory.get(scene_id)
                tracks = [[d for d in t if scene['interval'][0] <= d['pts'] < scene['interval'][1]] for t in tracked['data']]
                occurrences.extend(register_tracks(memory, [t for t in tracks if t], scene_id,
                                                   self.namespace, evidence['id'], cfg['tracking']))
            count = cfg['materialization']['max_entities_per_batch']
            ordered = sorted(occurrences, key=lambda o: (o['category'] != 'person', o['id']))
            requirements = [memory.get(r) for r in task['requirement_ids']]
            if (scope['mode'] == 'goal' and requirements and any(r['roles'] for r in requirements)
                    and all(r['quantifier'] in ('exists', 'last') for r in requirements) and len(ordered) > 4):
                # Goal-lazy extraction limits relation work to candidate targets and interaction context.
                # Detection records stay intact; unselected entities remain unknown for this goal.
                from .tracking import cosine, iou
                descriptions = [v['description'] for v in task.get('variables', [])
                                if v['name'] in {name for r in requirements for name in r['roles'].values()}]
                if descriptions:
                    embedded, _ = self.meter.call('embed', {'texts': descriptions+[o['category']+' '+o['description'] for o in ordered]}, task['id'])
                    queries = embedded['vectors'][:len(descriptions)]
                    vectors = embedded['vectors'][len(descriptions):]
                    selected_ids = set()
                    for query_vector in queries:
                        ranked = sorted(zip(ordered, vectors), key=lambda pair: (-cosine(query_vector, pair[1]), pair[0]['id']))
                        selected_ids.update(o['id'] for o, _ in ranked[:min(5, count//2)])
                    targets = [o for o in ordered if o['id'] in selected_ids]
                    # Keep people and nearby objects as context without declaring omitted objects absent.
                    context = [o for o in ordered if o['id'] not in selected_ids]
                    def proximity(o):
                        return max((iou(a['bbox'], b['bbox']) for a in o['boxes'] for t in targets for b in t['boxes']
                                    if a['frame_id'] == b['frame_id']), default=0.)
                    context.sort(key=lambda o: (o['category'] != 'person', -proximity(o), o['id']))
                    ordered = targets+context
            completed_entities = {oid for old in memory.tables['build_records'].values()
                                  if old['id'] != record['id'] and old['input_hash'] == key
                                  for oid in old.get('processed_occurrence_ids', [])}
            ordered = [o for o in ordered if o['id'] not in completed_entities]
            memory.update('build_records', record['id'], pending_occurrence_ids=[o['id'] for o in ordered],
                          processed_occurrence_ids=sorted(completed_entities))
            batches = [ordered[i:i+count] for i in range(0, len(ordered), count)] or [[]]
            for batch in batches:
                # Context overflow must split entities explicitly, retaining all frames and all entities.
                def extract_batch(entities):
                    try:
                        report, _ = self.meter.json('extract', {'frames': frames, 'subtitles': subtitles, 'interval': interval,
                                                               'occurrences': entities, 'extraction_scope': scope, 'goal': task['goal'],
                                                               **focus, 'max_claims': 3},
                                                   ('descriptions', 'claims', 'missing_entity', 'uncertainties'), task['id'])
                        return report
                    except ToolFailure as exc:
                        if 'Context overflow' not in str(exc) or len(entities) <= 1:
                            raise
                        middle = len(entities)//2
                        left, right = extract_batch(entities[:middle]), extract_batch(entities[middle:])
                        return {field: left[field]+right[field] for field in ('descriptions', 'claims', 'missing_entity', 'uncertainties')}
                report = extract_batch(batch)
                memory.update('build_records', record['id'], observation_gaps=list(dict.fromkeys(
                    memory.get(record['id'])['observation_gaps'] + report['uncertainties'])))
                for desc in report['descriptions']:
                    if desc['occurrence_id'] not in {o['id'] for o in batch} or not set(desc['source_frame_ids']) <= {f['frame_id'] for f in frames}:
                        raise ToolFailure('Entity description contains unauthorized references')
                    memory.update('occurrences', desc['occurrence_id'], description=desc['description'])
                for proposed in report['claims']:
                    claim = self.verify_and_add_claim(task, proposed, evidence, batch)
                    if claim:
                        result_ids.append(claim['id'])
                # Missing entities remain explicit gaps rather than invented Occurrence IDs.
                if report['missing_entity']:
                    self.meter.ledger.calls[-1]['missing_entity'] = report['missing_entity']
                completed_entities.update(o['id'] for o in batch)
                memory.update('build_records', record['id'], processed_occurrence_ids=sorted(completed_entities),
                              pending_occurrence_ids=[o['id'] for o in ordered if o['id'] not in completed_entities])
            for occurrence in occurrences:
                if any(occurrence['id'] in c['roles'].values() and memory.is_valid(c['id'])
                       for c in memory.tables['claims'].values()):
                    link = self.resolver.resolve(memory.get(occurrence['id']), task['id'])
                    if link:
                        result_ids.append(link['id'])
            if request.get('core'):
                report, cov_call = self.meter.json('coverage', {'frames': frames, 'core_interval': request['core'],
                                                              'goal': task['goal'], 'occurrences': occurrences,
                                                              'bindings': [memory.get(b) for b in task['target_binding_ids']],
                                                              'requirements': [memory.get(r) for r in task['requirement_ids']]},
                                                  ('visibility', 'result', 'min_target_short_side', 'task_verified',
                                                   'distinguishing_facts', 'source_frame_ids', 'unresolved_conflicts'), task['id'])
                if any(intersect(request['core'], gap) for gap in memory.manifest.get('missing_intervals', [])):
                    report.update(visibility='unknown', task_verified=False)
                    report['unresolved_conflicts'].append('Original video contains an undecodable range')
                if not set(report['source_frame_ids']) <= {f['frame_id'] for f in frames} or not report['source_frame_ids'] or report['unresolved_conflicts']:
                    report['task_verified'] = False
                target_entities = {memory.get(b)['entity_id'] for b in task['target_binding_ids'] if memory.is_valid(b)}
                if target_entities:
                    sizes = {}
                    effective_links = ([(l['id'], l) for l in memory.tables['identity_links'].values()]+
                        [(n['id'], n['content']) for n in memory.tables['derived_claims'].values()
                         if ':identity_use' in n['id']])
                    for ref, link in effective_links:
                        if link.get('entity_id') not in target_entities or not memory.is_valid(ref):
                            continue
                        occurrence = memory.get(link['occurrence_id'])
                        for box in occurrence['boxes']:
                            if not request['core'][0] <= box['pts'] < request['core'][1]:
                                continue
                            for frame in frames:
                                if frame['frame_id'] != box['frame_id']:
                                    continue
                                region = frame['crop'] or [0., 0., 1., 1.]
                                view = next((v for v in cov_call.get('model_frame_views', [])
                                             if v['frame_id'] == frame['frame_id']), frame)
                                short = min((box['bbox'][2]-box['bbox'][0])*view['width']/(region[2]-region[0]),
                                            (box['bbox'][3]-box['bbox'][1])*view['height']/(region[3]-region[1]))
                                sizes[(link['entity_id'], box['pts'])] = max(short, sizes.get((link['entity_id'], box['pts']), 0.))
                    expected_pts = {f['pts'] for f in frames if request['core'][0] <= f['pts'] < request['core'][1]}
                    if any((entity, pts) not in sizes for entity in target_entities for pts in expected_pts):
                        report.update(visibility='partial', task_verified=False)
                    report['min_target_short_side'] = min(sizes.values()) if sizes else None
                elif any(memory.get(r)['roles'] for r in task['requirement_ids']):
                    report.update(visibility='unknown', min_target_short_side=None, task_verified=False)
                verifier = obj(memory.new_id(self.namespace, 'coverage_verify'), node_type='query_fact',
                               content=report, support_sets=[[evidence['id']]],
                               status='supported' if report['task_verified'] else 'unresolved')
                memory.put('derived_claims', verifier)
                cov = coverage_record(memory.new_id(self.namespace, 'cov'), request['core'], task['requirement_ids'],
                                      evidence, verifier['id'], report, cfg, memory.manifest['frames'])
                memory.put('coverage', cov)
                coverage_ids.append(cov['id'])
            memory.update('build_records', record['id'], evidence_ids=[evidence['id']], result_ids=result_ids,
                          status='complete', unchecked_intervals=[], failure_reason=None,
                          call_ids=[c['call_id'] for c in self.meter.ledger.calls[start_call:]],
                          actual_cost=self.meter.ledger.actual-start_cost)
            record = memory.get(record['id'])
            dependencies = [evidence['id']] + [r for r in result_ids if memory.is_valid(r)]
            self.cache.save(key, 'outputs', evidence['input_hash'], model_hash, prompt_hash('extract'), spec,
                            result_ids, dependencies, record['call_ids'], task['target_signature'], scope,
                            payload={'build_record_id': record['id'], 'coverage_ids': coverage_ids})
            verified = [r for r in result_ids if r in memory.tables['claims'] and memory.is_valid(r)]
            if verified:
                self.cache.save(key, 'verified_facts', evidence['input_hash'], model_hash, prompt_hash('verify_claim'), spec,
                                verified, [evidence['id'], *verified], record['call_ids'], task['target_signature'], scope)
            memory.validate()
            return [evidence['id']], record, coverage_ids
        except (ToolFailure, ResourceExhausted, ValueError) as exc:
            memory.update('build_records', record['id'], status='partial', evidence_ids=[evidence['id']],
                          result_ids=result_ids, unchecked_intervals=[interval], failure_reason=str(exc),
                          actual_cost=self.meter.ledger.actual-start_cost,
                          call_ids=[c['call_id'] for c in self.meter.ledger.calls[start_call:]])
            raise ToolFailure(str(exc)) from exc

    def verify_and_add_claim(self, task, candidate, evidence, occurrences, verification_report=None, verification_operation='verify_claim'):
        memory = self.memory
        predicate = candidate.get('predicate')
        if predicate not in task['extraction_scope']['predicates']:
            return None
        if predicate not in PREDICATE_ROLES:
            return None
        roles = candidate.get('roles', {})
        if not isinstance(roles, dict):
            raise ToolFailure('Claim roles must be a mapping')
        allowed_roles = {o['id'] for o in occurrences}
        complete_roles = (not (set(PREDICATE_ROLES[predicate])-set(roles))
                          and all(role == 'content' or ref in allowed_roles for role, ref in roles.items()))
        interval = candidate.get('interval')
        if not interval or intersect(interval, evidence['interval']) != interval:
            raise ToolFailure('Claim interval exceeds observed package')
        frame_ids = candidate.get('source_frame_ids', [])
        actual = {f['frame_id'] for f in evidence['frame_inputs']}
        if not set(frame_ids) <= actual:
            raise ToolFailure('Claim refers to an unread frame')
        interval_frames = {f['frame_id'] for f in evidence['frame_inputs']
                           if interval[0] <= f['pts'] < interval[1] and f.get('visual_usable', True)}
        source_type = candidate.get('source_type', 'visual')
        if source_type not in ('visual', 'subtitle'):
            raise ToolFailure('Unsupported claim modality')
        role_occurrences = [o for o in occurrences if o['id'] in roles.values()]
        localized = all(role == 'content' or any(
            o['id'] == ref and any(b['frame_id'] in interval_frames and interval[0] <= b['pts'] < interval[1]
                                  for b in o['boxes']) for o in role_occurrences)
            for role, ref in roles.items())
        report = verification_report
        if report is None:
            report, _ = self.meter.json('verify_claim', {'frames': [f for f in evidence['frame_inputs'] if f.get('visual_usable', True)], 'subtitles': evidence['text_spans'],
                                                   'claim': candidate, 'occurrences': role_occurrences,
                                                   'require_complete_claim': True},
                                   ('result', 'distinguishing_facts', 'unresolved_conflicts', 'source_frame_ids', 'process_visible'), task['id'])
        status = report['result']
        if status not in ('supported', 'refuted', 'unresolved'):
            raise ToolFailure('Invalid semantic verification status')
        report = deepcopy(report)
        fact_type = candidate.get('fact_type', 'action' if predicate == 'other' or predicate in DYNAMIC else 'state')
        requires_process = predicate in DYNAMIC or fact_type == 'action'
        if (report.get('whole_claim_supported') is False and status == 'supported'
                or not complete_roles or not localized or not frame_ids or not set(frame_ids) <= interval_frames or not set(report['source_frame_ids']) <= interval_frames
                or not report['source_frame_ids'] or report['unresolved_conflicts']
                or (source_type == 'subtitle' and predicate != 'speech_claim')
                or (source_type == 'visual' and fact_type == 'inference')
                or (requires_process and (len(set(frame_ids)) < 3 or len(set(report['source_frame_ids'])) < 3 or not report['process_visible']))):
            status = 'unresolved'
        if candidate.get('observed_at') is not None and not interval[0] <= candidate['observed_at'] < interval[1]:
            status = 'unresolved'
        report['result'] = status
        new_verification = verification(report, [evidence['id']], self.cfg, verification_operation)
        # Reuse identity, not an obsolete judgment. Conflicting rechecks invalidate
        # downstream proofs and remain unresolved until independent evidence arrives.
        for old in list(memory.tables['claims'].values()):
            if predicate == 'other' and old.get('description', '').strip().casefold() != candidate.get('description', '').strip().casefold():
                continue
            sources = set(old.get('source_frame_ids', []))
            if (old['predicate'] == predicate and old['roles'] == roles and old['interval'] == interval
                    and old['source_type'] == source_type and old.get('fact_type', fact_type) == fact_type
                    and old.get('observed_at') == candidate.get('observed_at') and set(frame_ids) == sources):
                history = (old.get('verification_history') or [old['verification']]) + [new_verification]
                judgments = {v['result'] for v in history}
                if {'supported', 'refuted'} <= judgments:
                    status = 'unresolved'
                    new_verification = dict(new_verification, result=status,
                        unresolved_conflicts=list(dict.fromkeys(new_verification['unresolved_conflicts'] +
                            ['Conflicting verification judgments on the same source frames'])))
                if old['status'] == 'supported' and status != 'supported':
                    memory.revoke(old['id'])
                sources = list(dict.fromkeys(old['evidence_ids']+[evidence['id']]))
                return memory.update('claims', old['id'], status=status, fact_type=fact_type,
                    verification=new_verification, verification_history=history, evidence_ids=sources,
                    support_sets=[[e] for e in sources] if status == 'supported' else [],
                    counterevidence_ids=list(dict.fromkeys(old['counterevidence_ids']+
                        ([evidence['id']] if report['result'] == 'refuted' else []))))
        duplicate = None
        for old in list(memory.tables['claims'].values()):
            if (old['predicate'] != predicate or old['roles'] != roles or old['source_type'] != source_type
                    or old.get('fact_type', fact_type) != fact_type):
                continue
            if predicate == 'other' and old.get('description', '').strip().casefold() != candidate.get('description', '').strip().casefold():
                continue
            overlap = intersect(old['interval'], interval)
            if overlap:
                temporal_iou = (overlap[1]-overlap[0])/(max(interval[1], old['interval'][1])-min(interval[0], old['interval'][0]))
                if (temporal_iou >= .5 and set(frame_ids) & set(old.get('source_frame_ids', []))
                        and old['status'] == status == 'supported'):
                    merged_sources = list(dict.fromkeys(old['evidence_ids']+[evidence['id']]))
                    return memory.update('claims', old['id'], interval=[min(interval[0], old['interval'][0]), max(interval[1], old['interval'][1])],
                                         evidence_ids=merged_sources, support_sets=[merged_sources],
                                         verification=new_verification,
                                         verification_history=(old.get('verification_history') or [old['verification']])+[new_verification],
                                         source_frame_ids=sorted(set(frame_ids) | set(old.get('source_frame_ids', []))))
                # Ambiguous repetition is recorded, never silently collapsed for counting.
                duplicate = old['duplicate_group'] or memory.new_id(self.namespace, 'possible_duplicate')
                memory.update('claims', old['id'], duplicate_group=duplicate)
        report['result'] = status
        claim = obj(memory.new_id(self.namespace, 'claim'), scene_ids=task['scene_ids'], predicate=predicate, roles=roles,
                    interval=interval, observed_at=candidate.get('observed_at'), status=status, source_type=source_type,
                    evidence_ids=[evidence['id']], counterevidence_ids=[evidence['id']] if status == 'refuted' else [],
                    support_sets=[[evidence['id']]] if status == 'supported' else [],
                    fact_type=fact_type, verification=new_verification, verification_history=[new_verification],
                    duplicate_group=duplicate, description=candidate.get('description', ''), source_frame_ids=frame_ids)
        memory.put('claims', claim)
        return claim

    @metered_cpu('feedback_maintenance')
    def integrate_assessment(self, task, report):
        memory = self.memory
        from .updater import apply_identity_suggestions, apply_cluster_suggestions
        self.last_patch_ids = apply_identity_suggestions(memory, report.get('identity_corrections', []),
                                                         self.meter, self.cfg, self.namespace, task['id'])
        self.last_patch_ids += apply_cluster_suggestions(memory, report.get('identity_operations', []),
                                                         self.meter, self.cfg, self.namespace, task['id'])
        for discrimination in report['candidate_discriminations']:
            claims = discrimination.get('claim_ids', [])
            if (discrimination.get('result') == 'different' and discrimination.get('hypothesis_id') in memory.tables['bindings']
                    and claims and all(c in memory.tables['claims'] and memory.is_valid(c) for c in claims)):
                memory.revoke(discrimination['hypothesis_id'])
        variable_names = {v['name'] for v in task.get('variables', [])}
        for suggestion in report['bindings']:
            variable, occurrence_id = suggestion['variable'], suggestion['occurrence_id']
            claim_ids = suggestion['claim_ids']
            if (variable not in variable_names or occurrence_id not in memory.tables['occurrences']
                    or not claim_ids or not all(memory.is_valid(c) and c in memory.tables['claims'] for c in claim_ids)
                    or not any(occurrence_id in memory.get(c)['roles'].values() for c in claim_ids)):
                continue
            links = [l for l in memory.tables['identity_links'].values()
                     if l['occurrence_id'] == occurrence_id and memory.is_valid(l['id'])]
            uses = [u for u in memory.tables['derived_claims'].values() if memory.is_valid(u['id'])
                    and u['content'].get('occurrence_id') == occurrence_id and 'entity_id' in u['content']]
            if not links and not uses:
                continue
            link = links[0] if links else {'id': uses[-1]['id'], 'entity_id': uses[-1]['content']['entity_id']}
            current = [b for b in memory.tables['bindings'].values() if b['variable'] == variable and memory.is_valid(b['id'])]
            if current:
                # Replacement requires a separately sourced discrimination below.
                continue
            memory.put('bindings', obj(memory.new_id(self.namespace, 'binding'), variable=variable,
                                       entity_id=link['entity_id'], anchor_occurrence_ids=[occurrence_id],
                                       support_sets=[[link['id'], *claim_ids]], status='confirmed'))
        discriminations = []
        for d in report['candidate_discriminations']:
            if (d['hypothesis_id'] not in memory.tables['bindings'] or not d['claim_ids']
                    or not all(c in memory.tables['claims'] and memory.is_valid(c) for c in d['claim_ids'])):
                continue
            evidence_ids = list(dict.fromkeys(e for c in d['claim_ids'] for e in memory.get(c)['evidence_ids']))
            discriminations.append({'hypothesis_id': d['hypothesis_id'], 'result': d['result'], 'evidence_ids': evidence_ids})
            if d['result'] == 'different':
                memory.revoke(d['hypothesis_id'])
        updates = []
        for update in report['requirement_updates']:
            requirement_id, claims = update['requirement_id'], update['claim_ids']
            if requirement_id not in task['requirement_ids'] or update['result'] != 'supported' or not claims:
                continue
            requirement = memory.get(requirement_id)
            if not all(c in memory.tables['claims'] and memory.is_valid(c) for c in claims):
                continue
            if requirement['predicate'] != 'other' and not any(memory.get(c)['predicate'] == requirement['predicate'] for c in claims):
                continue
            if any(memory.get(c)['source_type'] not in requirement['acceptable_modalities'] for c in claims):
                continue
            entity_variables = {v for role, v in requirement['roles'].items() if role != 'content'}
            bindings = [b for b in memory.tables['bindings'].values() if memory.is_valid(b['id'])
                        and b['variable'] in entity_variables]
            if entity_variables - {b['variable'] for b in bindings}:
                continue
            if any(not memory.is_valid(p) for p in requirement['prerequisite_ids']):
                continue
            if not all(intersect(memory.get(c)['interval'], requirement['scope']) == memory.get(c)['interval'] for c in claims):
                continue
            effective_links = ([(l['id'], l) for l in memory.tables['identity_links'].values() if memory.is_valid(l['id'])]+
                               [(u['id'], u['content']) for u in memory.tables['derived_claims'].values()
                                if ':identity_use' in u['id'] and memory.is_valid(u['id'])])
            qualified = []
            for claim_id in claims:
                claim = memory.get(claim_id)
                if claim['predicate'] != requirement['predicate']:
                    continue
                role_links = []
                for role, variable in requirement['roles'].items():
                    if role == 'content':
                        continue
                    binding = next(b for b in bindings if b['variable'] == variable)
                    matches = [ref for ref, link in effective_links if
                               link.get('occurrence_id') == claim['roles'].get(role) and
                               link.get('entity_id') == binding['entity_id']]
                    if not matches:
                        break
                    role_links.extend(matches)
                else:
                    qualified.append((claim_id, role_links))
            if not qualified:
                continue
            if requirement['quantifier'] == 'global':
                alternatives = [(claims, [ref for _, links in qualified for ref in links])]
            else:
                alternatives = [([claim_id], links) for claim_id, links in qualified]
            groups = deepcopy(requirement['support_sets'])
            added_refs = []
            for primary_claims, links in alternatives:
                group = list(dict.fromkeys(primary_claims+[b['id'] for b in bindings]+links+requirement['prerequisite_ids']))
                if group not in groups:
                    groups.append(group)
                added_refs.extend(group)
            memory.update('requirements', requirement_id, support_sets=groups, status='satisfied')
            updates.append({'requirement_id': requirement_id, 'result': 'supported', 'basis_refs': list(dict.fromkeys(added_refs))})
        leads = [l for l in report['next_leads'] if l.get('scene_id') in memory.tables['scenes']
                 and l.get('requirement_id') in memory.tables['requirements']
                 and l.get('route') in ('summary', 'entity', 'time', 'event', 'recovery')
                 and l.get('basis_refs') and all(memory.get(r, None) is not None for r in l['basis_refs'])]
        return updates, discriminations, leads
