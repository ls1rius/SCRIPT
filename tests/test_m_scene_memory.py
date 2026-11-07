from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script_core import adaptive_transition as navigation, partial_temporal, m_scene_memory as core
from script_core.config import load_config
from script_core.schemas import output_schema, validate_output
from tests.test_merged_lazy_memory import Backend as HBackend


def step(action='stop', **updates):
    value = dict(sufficient=action == 'stop', reason='Evidence checked', gap='', action=action,
        scene_id='stop', caption_ids=[], entity_ids=[], focus=[], fields=[],
        current_scene_assessment={'stop':'sufficient', 'extract':'needs_local_detail',
                                  'read_scene':'cannot_resolve_locally'}[action], local_unavailable_reason='')
    value.update(updates)
    value.setdefault('option_comparison','The choices differ in the observed detail.')
    value.setdefault('option_assessments',{f'O{i}':dict(
        status=('supported' if i==0 else 'ruled_out') if value['sufficient'] else 'unresolved',
        source_caption_ids=['C3'] if value['sufficient'] else [],reason='Current observation checked')
        for i in range(5)})
    return value


def navigation_wire(plan):
    value=deepcopy(plan)
    value['evidence_status']='sufficient' if value.pop('sufficient') else 'insufficient'
    value['supported_option']=next((k for k,v in value['option_assessments'].items()
        if v['status']=='supported'),'O0') if value['evidence_status']=='sufficient' else 'unresolved'
    return value


def fixture():
    captions = [dict(id=f'C{i}', interval=[i,i+1], text=text) for i,text in enumerate([
        'An irrelevant chair is blue.', '#C The book is wet.', '#C The book is wet.',
        '#C C moves the red book with her left hand.'])]
    packet = dict(captions=captions, question=dict(question_id='q', video_id='v',
        question='What does C primarily do throughout the video?',
        options=['left hand','right hand','both','neither','unknown']))
    directory = dict(captions=captions, scenes=[dict(id=f'S{i}', interval=[i,i+1],
        center=[1.,0.], caption_ids=[f'C{i}'], chronological_order=[i],
        description=['irrelevant chair','wet book','book condition','moves book'][i]) for i in range(4)], pointers=[])
    return packet, directory


class Backend(HBackend):
    def __init__(self, plans=None, entry='S3', invalid_planner=False,candidate_order=None):
        super().__init__()
        self.plans = deepcopy(plans or [step()])
        self.entry = entry
        self.invalid_planner = invalid_planner
        self.candidate_order=[entry,'S1'] if candidate_order is None else candidate_order

    def call(self, operation, inputs, *args):
        if not operation.startswith('m_') and operation != 'transition_extract':
            return super().call(operation, inputs, *args)
        self.calls.append((operation, deepcopy(inputs)))
        if operation == 'm_candidate_observe':
            return dict(data=dict(scene_observations={s['id']:s['description'] for s in inputs['directory']}))
        if operation == 'm_candidate_query':
            return dict(data=dict(option_claims={f'O{i}':dict(observable_claim=o,proposed_explanation='')
                for i,o in enumerate(inputs['options'])},retrieval_question=inputs['question']))
        if operation == 'm_candidate_rank':
            order=list(dict.fromkeys(self.candidate_order))
            return dict(data=dict(scene_reviews={s['id']:dict(anchor=s['summary_clauses'][0] if s['id'] in order else None,
                evidence_level='task_evidence' if s['id']==self.entry and s['id'] in order else 'context_only' if s['id'] in order else 'unrelated') for s in inputs['directory']}))
        if operation == 'm_select_scene':
            order=list(dict.fromkeys(self.candidate_order))
            return dict(data=dict(reason='Only related scene summaries are candidates',
                scene_decisions={s['id']:dict(relevance='candidate' if s['id'] in order else 'excluded',
                    priority=100-order.index(s['id']) if s['id'] in order else 0,
                    reason='Related activity' if s['id'] in order else 'Unrelated background') for s in inputs['directory']}))
        if operation in ('m_transition_next','m_resolve_gap'):
            plan=self.plans.pop(0)
            if operation=='m_resolve_gap':
                for key in ('sufficient','option_comparison','option_assessments'):plan.pop(key,None)
                plan['resolution_status']='unresolved'
            else:plan=navigation_wire(plan)
            return dict(data=plan)
        if operation == 'm_sufficiency_check':
            return dict(data=dict(reason='Observed value excludes alternative',verdict='ruled_out',
                source_caption_ids=[inputs['captions'][0]['id']],missing_detail=''))
        if operation == 'transition_extract':
            c = inputs['captions'][0]['id']
            return dict(data=dict(entities=[dict(entity_ref='new', label='book', source_caption_ids=[c],
                attributes=[], observations=[dict(caption_id=c,kind='state')])], relations=[], unresolved=''))
        if operation == 'm_evidence_plan':
            keys = list(inputs['schema']['properties'])
            return dict(raw=json.dumps({k:'skip' if self.invalid_planner else 'keep' for k in keys}))
        if operation == 'm_answer':
            return dict(data=dict(selected_option='0', raw='prediction: A'))
        raise AssertionError(operation)


class MSceneTests(unittest.TestCase):
    def search(self, backend, target):
        packet, directory = fixture()
        cfg = load_config('configs/script_M.yaml')
        cfg['query']['temporal_sparse_visual'] = False
        result = navigation.run(packet, directory, [], backend, cfg, target,
            dict(online_seconds=1500, max_scene_visits=4), evidence_only=True)
        return packet, cfg, result

    def test_summary_selection_and_single_scene_sufficiency_ignore_global_thirds(self):
        with tempfile.TemporaryDirectory() as td:
            backend = Backend()
            _, _, result = self.search(backend, Path(td))
            self.assertIsNone(result['error'], result['error'])
            self.assertEqual(result['opened_scenes'], ['S3'])
            self.assertEqual(result['selected_seconds'], [3])
            self.assertEqual(result['stop_reason'], 'sufficient')
            self.assertFalse(result['coverage_check']['enforced'])
            self.assertEqual([op for op,_ in backend.calls if op!='m_sufficiency_check'],
                             ['m_candidate_query','m_candidate_rank']+['m_transition_next'])
            self.assertEqual([op for op,_ in backend.calls].count('m_sufficiency_check'),4)
            brief, *selections, review = [inp for op,inp in backend.calls if op!='m_sufficiency_check']
            selection=dict(directory=[s for item in selections for s in item['directory']])
            self.assertEqual(len(selection['directory']), 4)
            self.assertEqual(review['current_scene'], 'S3')
            self.assertEqual([c['id'] for c in review['captions']], ['C3'])
            self.assertNotIn('coverage', review)

    def test_local_inspection_before_gap_driven_expansion_and_stop(self):
        plans = [step('extract', sufficient=False, gap='Need book condition', caption_ids=['C3'],
                      focus=['book'], fields=['state']),
                 step('read_scene', sufficient=False, gap='Condition before the movement', scene_id='S1',
                      local_unavailable_reason='Current scene contains only the later movement'), step()]
        with tempfile.TemporaryDirectory() as td:
            backend = Backend(plans)
            _, _, result = self.search(backend, Path(td))
            self.assertIsNone(result['error'], result['error'])
            self.assertEqual(result['opened_scenes'], ['S3','S1'])
            self.assertEqual(result['selected_seconds'], [1,3])
            self.assertEqual([op for op,_ in backend.calls if op!='m_sufficiency_check'], [
                'm_candidate_query','m_candidate_rank']+['m_transition_next','transition_extract','m_transition_next','m_transition_next'])
            reviews = [inp for op,inp in backend.calls if op == 'm_transition_next']
            self.assertEqual([c['id'] for c in reviews[-1]['captions']], ['C1'])
            self.assertEqual([c['id'] for c in reviews[-1]['previous_scene_captions']], ['C3'])
            self.assertTrue(reviews[-1]['observations'])
            self.assertEqual(reviews[-1]['previous_reviews'][-1]['gap'], 'Condition before the movement')

    def test_expansion_without_gap_or_local_inability_fails_before_opening(self):
        for updates in [dict(gap=''), dict(local_unavailable_reason=''),
                        dict(current_scene_assessment='needs_local_detail'), dict(scene_id='S3')]:
            plan = step('read_scene', sufficient=False, gap='Earlier condition', scene_id='S1',
                        local_unavailable_reason='Not described locally')
            plan.update(updates)
            with self.subTest(updates=updates), tempfile.TemporaryDirectory() as td:
                backend = Backend([plan])
                _, _, result = self.search(backend, Path(td))
                self.assertIsNotNone(result['error'])
                self.assertEqual(result['opened_scenes'], ['S3'])

    def test_local_source_allowlist_rejects_unopened_or_previous_scene_sources(self):
        inp = dict(entities=[], captions=[dict(id='C3')], allowed_scene_ids=['S0','S1','S2'],
                   options=['left','right','both','neither','unknown'])
        plan = step('extract', sufficient=False, gap='detail', caption_ids=['C0'], focus=['book'], fields=['state'])
        with self.assertRaises(ValueError): core.validate_step(navigation_wire(plan), inp)
        schema = output_schema('m_select_scene', dict(directory=[dict(id='S3')]))
        with self.assertRaises(ValueError): validate_output(dict(scene_id='S0',reason='not allowed'), schema)

    def test_unresolved_stop_and_scene_budget_do_not_force_more_reads(self):
        for plan in [step(sufficient=False, gap='No useful evidence', current_scene_assessment='no_useful_step'),
                     step('read_scene', sufficient=False,gap='Earlier state',scene_id='S1',
                          local_unavailable_reason='Absent locally')]:
            with self.subTest(plan=plan), tempfile.TemporaryDirectory() as td:
                backend = Backend([plan],candidate_order=['S3'] if plan['action']=='stop' else ['S3','S1'])
                packet,directory = fixture()
                cfg = load_config('configs/script_M.yaml');cfg['query']['temporal_sparse_visual']=False
                result = navigation.run(packet,directory,[],backend,cfg,td,
                    dict(online_seconds=1500,max_scene_visits=1,m_max_scene_visits=1),evidence_only=True)
                self.assertIsNone(result['error'],result['error'])
                self.assertEqual(result['opened_scenes'],['S3'])
                self.assertEqual(len(backend.calls),3)

    def test_candidate_exhaustion_stops_without_opening_excluded_scenes(self):
        plans=[step('read_scene',sufficient=False,gap='Need earlier state',scene_id='S1',
                    local_unavailable_reason='Earlier state absent locally'),
               step(sufficient=False,gap='Still cannot distinguish options',current_scene_assessment='no_useful_step')]
        with tempfile.TemporaryDirectory() as td:
            backend=Backend(plans,candidate_order=['S3','S1'])
            _,_,result=self.search(backend,Path(td))
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['opened_scenes'],['S3','S1'])
            self.assertEqual(result['stop_reason'],'candidate_exhausted')
            self.assertFalse(result['reviews'][-1]['sufficient'])
            trace=io.read(Path(td)/'navigation_trace.json')
            self.assertEqual(trace['remaining_candidates'],[])
            self.assertEqual([s['id'] for s in trace['scene_catalog']],['S0','S1','S2','S3'])
            self.assertEqual([e['scene_id'] for e in trace['events'] if e['event']=='scene_opened'],['S3','S1'])

    def test_excluded_scene_cannot_be_selected_for_expansion(self):
        plan=step('read_scene',sufficient=False,gap='Need detail',scene_id='S0',
                  local_unavailable_reason='Absent here')
        with tempfile.TemporaryDirectory() as td:
            backend=Backend([plan],candidate_order=['S3','S1'])
            _,_,result=self.search(backend,Path(td))
            self.assertIsNotNone(result['error'])
            self.assertEqual(result['opened_scenes'],['S3'])
            request=next(inp for op,inp in backend.calls if op=='m_transition_next')
            self.assertEqual(request['allowed_scene_ids'],['S1'])
            self.assertEqual([s['id'] for s in request['directory']],['S1','S3'])

    def test_empty_candidate_set_never_opens_scene_and_marks_answer_insufficient(self):
        from script_core.scheme4 import answer
        from script_core.budget import BudgetLedger
        with tempfile.TemporaryDirectory() as td:
            packet,directory=fixture();packet.update(feature_hash='fixture',frames=[])
            directory['cost_summary']=BudgetLedger(600,answer_reserve=0).summary()
            root=Path(td);io.write(root/'directory.json',directory)
            cfg=load_config('configs/script_M.yaml');cfg['query']['temporal_sparse_visual']=False
            backend=Backend(candidate_order=[])
            result=answer(packet,[],cfg,backend,root)
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['selected_seconds'],[])
            self.assertEqual(result['stop_decision']['evidence_status'],'insufficient')
            ops=[op for op,_ in backend.calls]
            self.assertEqual(ops.count('m_answer'),1)
            self.assertNotIn('m_transition_next',ops)
            self.assertNotIn('h_plan_fields',ops)
            self.assertNotIn('m_evidence_plan',ops)
            self.assertEqual(io.read(root/'partial_search/result.json')['stop_reason'],'no_relevant_candidates')

    def test_initial_shortlist_partition_and_order_are_audited(self):
        packet,directory=fixture()
        inputs=dict(directory=directory['scenes'])
        valid=dict(scene_decisions={s['id']:dict(relevance='candidate' if s['id'] in ['S3','S1'] else 'excluded',
            priority={'S3':100,'S1':90}.get(s['id'],0),
            reason='Related' if s['id'] in ['S3','S1'] else 'Unrelated') for s in directory['scenes']},
            reason='Ranked by relevance')
        self.assertEqual(core.validate_candidates(valid,inputs),['S3','S1'])
        for sid,priority in [('S0',80),('S3',0),('S3',101)]:
            bad=deepcopy(valid);bad['scene_decisions'][sid]['priority']=priority
            with self.assertRaises(ValueError):core.validate_candidates(bad,inputs)
        tied=deepcopy(valid);tied['scene_decisions']['S1']['priority']=100
        self.assertEqual(core.validate_candidates(tied,inputs),['S1','S3'])

    def test_option_audit_blocks_partial_match_and_unobserved_sources(self):
        inp = dict(entities=[],captions=[dict(id='C3')],allowed_scene_ids=['S1'],
            options=['left','right','both','neither','unknown'])
        for status, sources in [('unresolved',['C3']),('ruled_out',[]),('ruled_out',['C0'])]:
            plan = step()
            plan['option_assessments']['O1'].update(status=status,source_caption_ids=sources)
            with self.subTest(status=status,sources=sources), self.assertRaises(ValueError):
                core.validate_step(navigation_wire(plan),inp)
        core.validate_step(navigation_wire(step()),inp)  # A single observed scene remains sufficient.

    def test_m_local_validation_failure_is_not_retried(self):
        plan = step('extract',sufficient=False,gap='Need condition',caption_ids=['C3'],
                    focus=['book'],fields=['state'])
        with tempfile.TemporaryDirectory() as td:
            backend=Backend([plan])
            with patch.object(navigation,'commit_delta',side_effect=ValueError('invalid source binding')):
                _,_,result=self.search(backend,Path(td))
            self.assertIsNotNone(result['error'])
            self.assertEqual([op for op,_ in backend.calls].count('transition_extract'),1)

    def test_pairwise_unresolved_alternative_returns_gap_to_navigation(self):
        class CheckedBackend(Backend):
            blocked=False
            def call(self,op,inp,*args):
                result=super().call(op,inp,*args)
                if op=='m_sufficiency_check' and not self.blocked:
                    self.blocked=True
                    result['data'].update(verdict='unresolved',missing_detail='Need earlier condition')
                return result
        plans=[step(),step('read_scene',sufficient=False,gap='Need earlier condition',scene_id='S1',
            local_unavailable_reason='Earlier condition is absent here'),step()]
        with tempfile.TemporaryDirectory() as td:
            backend=CheckedBackend(plans)
            _,_,result=self.search(backend,Path(td))
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['opened_scenes'],['S3','S1'])
            self.assertFalse(result['reviews'][0]['sufficient'])
            requests=[inp for op,inp in backend.calls if op in ('m_transition_next','m_resolve_gap')]
            self.assertTrue(requests[1]['sufficiency_blocked'])
            self.assertEqual(requests[1]['sufficiency_feedback'][0]['missing_detail'],'Need earlier condition')
            self.assertNotIn('sufficiency_blocked',requests[2])

    def test_retrieval_to_h_i_l_planning_and_one_constrained_answer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); backend = Backend()
            packet,cfg,search = self.search(backend,root/'search')
            self.assertIsNone(search['error'],search['error'])
            bundle = json.loads((root/'search/evidence_bundle.json').read_text())
            result = partial_temporal.run(packet,bundle,backend,cfg,root/'final',dict(online_seconds=1500))
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['memory_variant'],'M')
            self.assertEqual(result['qa_evidence']['parent_memory_variant'],'I')
            self.assertEqual(result['qa_evidence']['evidence_planner']['parent'],'L')
            self.assertIn('/M/I/h_script.sqlite3',result['qa_evidence']['database'])
            ops = [op for op,_ in backend.calls]
            self.assertEqual(ops.count('m_answer'),1)
            self.assertEqual(ops.count('m_evidence_plan'),1)
            self.assertNotIn('videotree_qa',ops)
            self.assertIn('h_plan_fields',ops)
            answer = next(inp['inputs'] for op,inp in backend.calls if op=='m_answer')
            self.assertNotIn('irrelevant chair',answer['prompts'][0])
            self.assertIn('left hand',io.parts(answer)[1])
            for op,inp in backend.calls:
                if op.startswith('h_extract'):
                    for request in inp.get('requests',[inp]):
                        self.assertNotIn('question',request)
                        self.assertNotIn('options',request)
            saved = json.loads((root/'final/M/final_input.json').read_text())
            self.assertEqual(saved,answer)

    def test_invalid_l_plan_is_audited_and_never_reaches_answer(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);backend=Backend(invalid_planner=True)
            packet,cfg,_=self.search(backend,root/'search')
            bundle=json.loads((root/'search/evidence_bundle.json').read_text())
            result=partial_temporal.run(packet,bundle,backend,cfg,root/'final',dict(online_seconds=1500))
            self.assertIsNotNone(result['error'])
            self.assertNotIn('m_answer',[op for op,_ in backend.calls])
            self.assertTrue((root/'final/M/planner_output.json').exists())

    def test_cli_mode_uses_m_with_l_default(self):
        import script_qa
        class Stop(Exception): pass
        captured=[]
        def preflight(cfg):captured.append(cfg);raise Stop
        with patch.object(script_qa,'preflight',side_effect=preflight), self.assertRaises(Stop):
            script_qa.main(['query','--base','unused','--questions','unused','--out','unused','--memory','M'])
        self.assertEqual(captured[0]['query']['temporal_qa_evidence'],core.MODE)
        self.assertEqual(captured[0]['query']['json_repair_retries'],0)
        self.assertEqual(load_config()['query']['temporal_qa_evidence'],'planned_lazy_script')

    def test_ordinary_query_dispatch_reports_semantic_sufficiency(self):
        from script_core.scheme4 import answer
        from script_core.budget import BudgetLedger
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);packet,directory=fixture()
            packet.update(feature_hash='cached-features',frames=[])
            directory['cost_summary']=BudgetLedger(600).summary()
            io.write(root/'directory.json',directory)
            cfg=load_config('configs/script_M.yaml');cfg['query']['temporal_sparse_visual']=False
            backend=Backend()
            result=answer(packet,[],cfg,backend,root)
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['stop_decision']['evidence_status'],'sufficient')
            self.assertEqual(result['selected_seconds'],[3])
            self.assertFalse(result['coverage_check']['enforced'])
            self.assertEqual(result['run_config']['query']['temporal_qa_evidence'],core.MODE)


if __name__ == '__main__': unittest.main()
