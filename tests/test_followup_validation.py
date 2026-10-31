"""Follow-up invariants: fixed evidence, strict sources, atomic updates and gates."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from script.run_followup_validation import direct_input, eligible_jobs, resume_terminal_states, stop_on_resource_error
from script.watch_followup_resources import stop_worker
from script_core.state import write_json
from script_core import adaptive_entity as adaptive, optimized_entity as pipeline, optimized_memory as memory
from script_core.backend import decode_structured_text
from script_core.schemas import validate_output
from script_core.videotree_tools import explicit_qa_answer
from tests.test_optimized_scheme4 import caps, fresh, entity, delta, commit, span


class FollowupTests(unittest.TestCase):
    def test_ablation_removes_only_memory_and_preserves_visual_suffix_exactly(self):
        prefix = 'Question and captions, unchanged.\n'
        suffix = '\n\nSupplemental sampled visual observations (verified):\n[{"text":"red"}]'
        original = dict(query_id='v', head='same instruction', prompts=[prefix+
            '\n\nTemporal entity memory (caption assertions):\n'+json.dumps({'entities':[{'text':'})\n'}]})+suffix])
        self.assertEqual(direct_input(original), dict(original,prompts=[prefix+suffix]))
        self.assertIn('Temporal entity memory', original['prompts'][0])
        with self.assertRaises(ValueError):
            direct_input(dict(original,prompts=[prefix]))

    def test_duplicate_existing_rows_preserve_both_mentions_and_aliases(self):
        sources = caps(['Alice holds a box.', 'Alice opens the box.'])
        m = fresh(sources)
        commit(m,delta(entity(sources,'Alice')))
        commit(m,delta(entity(sources,'Alice',ref='E0'),
            entity(sources,'Alice',cids=('C1',),ref='E0'), entity(sources,'box'),
            relations=[dict(subject='B1',object='B2',predicate=span(sources,'C1','opens'),update='present')]),key='refill')
        self.assertEqual(len(m['entities']),2)
        self.assertEqual(m['entities']['E0']['mention_caption_ids'],['C0','C1'])
        self.assertEqual(len(m['extraction_requests']),2)
        self.assertEqual(m['relation_records'][0]['subject'],'E0')
        self.assertEqual(m['relation_records'][0]['object'],'E1')

    def test_changed_existing_label_still_fails_atomically(self):
        sources=caps(['Alice holds a box.'])
        m=fresh(sources);commit(m,delta(entity(sources,'Alice')))
        before=deepcopy(m)
        with self.assertRaises(ValueError):
            commit(m,delta(entity(sources,'box',ref='E0')))
        self.assertEqual(m,before)

    def test_event_failure_rolls_back_entity_stage_before_retry(self):
        sources=caps(['Alice holds a box.']);m=fresh(sources);before=deepcopy(m)
        meter=Mock();meter.json.return_value=(delta(entity(sources,'Alice')),{'call_id':'call'})
        plan=dict(gap='action',focus=['Alice'],fields=['action'],depth='event')
        with patch.object(pipeline,'extract_events',side_effect=ValueError('bad event')):
            with self.assertRaises(ValueError):
                pipeline.extract_batch(m,sources,'S0',dict(question='What?',options=[]),plan,meter,set())
        self.assertEqual(m,before)

    def test_single_letter_is_explicit_but_conflicting_answers_are_rejected(self):
        self.assertEqual(explicit_qa_answer('B\n\nExplanation: C holds a box.'),1)
        self.assertEqual(explicit_qa_answer('B\nprediction: C'),-1)
        self.assertEqual(explicit_qa_answer('B could be right.'),-1)

    def test_lone_surrogates_are_rejected_without_replacing_text(self):
        with self.assertRaises(ValueError):decode_structured_text('{"unresolved":"\\udf00"}')
        with self.assertRaises(ValueError):validate_output('\udf00',{'type':'string'})
        self.assertEqual(decode_structured_text('{"x":"\\ud83d\\ude00"}'),{'x':'😀'})

    def test_compact_retry_requires_complete_json_and_preserves_fact_fields(self):
        sources=caps(['Alice holds a box.'])
        inp=dict(captions=sources,entities=[],fields=['action'],depth='entity',compact_diagnostics=True)
        schema=memory.output_schema('optimized_extract',inp)
        validate_output(delta(entity(sources,'Alice')),schema)
        with self.assertRaises(ValueError):validate_output(dict(delta(),unresolved='long prose'),schema)
        with self.assertRaises(ValueError):validate_output({'entities':[]},schema)

    def test_read_streak_inspects_matching_unprocessed_sources_only(self):
        sources=caps(['Alice holds a box.']);m=fresh(sources)
        plan=dict(action='read_scene',focus=['Alice'],fields=['action'],caption_ids=[],scene_id='S0')
        changed=adaptive.select_bounded_action(plan,sources,m,2,{'met':True})
        self.assertEqual(changed['action'],'extract')
        self.assertEqual(changed['caption_ids'],['C0'])
        m['extraction_requests']=[dict(caption_ids=['C0'],focus=['Alice'],fields=['action'])]
        self.assertEqual(adaptive.select_bounded_action(plan,sources,m,2,{'met':True}),plan)
        self.assertEqual(adaptive.select_bounded_action(plan,sources,fresh(sources),2,{'met':False}),plan)

    def test_query_attribute_quarantine_retains_source_valid_entity(self):
        sources=caps(['Alice holds a red box.']);m=adaptive.new_memory('v',sources,'hash')
        row=dict(entity_ref='new',label='box',source_caption_ids=['C0'],observations=[],
                 attributes=[dict(field='color',value='blue',source_caption_ids=['C0'])])
        report=dict(entities=[row],relations=[],unresolved='')
        adaptive.grounded_query_delta(m,report,sources,'S0',{'request_key':'x'},'call')
        self.assertEqual(m['entities']['E0']['attributes'],[])
        self.assertEqual(m['rejected_fields'][0]['kind'],'attribute')
        row['attributes'][0]['source_caption_ids']=['FOREIGN']
        before=deepcopy(m)
        with self.assertRaises(ValueError):
            adaptive.grounded_query_delta(m,report,sources,'S0',{'request_key':'x'},'call')
        self.assertEqual(m,before)

    def test_gate_dependencies_and_no_duplicate_launch(self):
        jobs=[dict(id='gate',depends=[]),dict(id='full',depends=['gate'])]
        self.assertEqual([j['id'] for j in eligible_jobs(jobs,{})],['gate'])
        self.assertEqual(eligible_jobs(jobs,{'gate':{'state':'running'}}),[])
        self.assertEqual(eligible_jobs(jobs,{'gate':{'state':'failed'}}),[])
        self.assertEqual([j['id'] for j in eligible_jobs(jobs,{'gate':{'state':'complete'}})],['full'])

    def test_repairs_take_priority_over_remaining_ablation_and_failed_gates_stay_blocked(self):
        jobs = [dict(id='direct', arm='partial_direct', gate=False, depends=[]),
                dict(id='optimized_gate', arm='optimized_scheme4', gate=True, depends=[]),
                dict(id='adaptive_gate', arm='adaptive_scheme4', gate=True, depends=[]),
                dict(id='optimized_full', arm='optimized_scheme4', gate=False, depends=['optimized_gate'])]
        self.assertEqual([j['id'] for j in eligible_jobs(jobs, {}, True)],
                         ['optimized_gate', 'adaptive_gate', 'direct'])
        states = dict(optimized_gate=dict(state='failed'), adaptive_gate=dict(state='complete'))
        self.assertEqual([j['id'] for j in eligible_jobs(jobs, states, True)], ['direct'])

    def test_idle_recovery_preserves_completed_and_failed_work_and_rejects_untracked_attempt(self):
        jobs = [dict(id='done', arm='partial_direct', gate=False, ids=['u'], depends=[]),
                dict(id='failed', arm='optimized_scheme4', gate=True, ids=['v'], depends=[]),
                dict(id='pending', arm='adaptive_scheme4', gate=True, ids=['w'], depends=[])]
        states = dict(done=dict(state='complete'), failed=dict(state='failed'))
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            write_json(out/'scheduler_state.json', states)
            write_json(out/'workers/done/completion.json', dict(completed=1, passed=True))
            result = out/'formal/arms/partial_direct/runs/u/result.json'
            write_json(result, dict(job_id='done', total_measured_seconds=2))
            self.assertEqual(resume_terminal_states(out, jobs), states)
            (out/'formal/arms/adaptive_scheme4/runs/w').mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'Untracked attempt'):
                resume_terminal_states(out, jobs)

    def test_recovery_refuses_active_or_inconsistent_jobs(self):
        job = dict(id='gate', arm='optimized_scheme4', gate=True, ids=['u'], depends=[])
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            write_json(out/'scheduler_state.json', dict(gate=dict(state='running', pid=123)))
            with self.assertRaisesRegex(ValueError, 'terminal known jobs'):
                resume_terminal_states(out, [job])
            write_json(out/'scheduler_state.json', dict(gate=dict(state='complete')))
            write_json(out/'workers/gate/completion.json', dict(completed=1, passed=False))
            with self.assertRaisesRegex(ValueError, 'completion evidence'):
                resume_terminal_states(out, [job])

    def test_query_invalid_relation_cannot_hide_valid_entity_or_invent_endpoints(self):
        sources=caps(['Alice holds a red box.']);m=adaptive.new_memory('v',sources,'hash')
        row=dict(entity_ref='new',label='box',source_caption_ids=['C0'],observations=[],attributes=[])
        report=dict(entities=[row],relations=[dict(subject='B0',object='B1',predicate='holds',
                    source_caption_ids=['C0'])],unresolved='')
        adaptive.grounded_query_delta(m,report,sources,'S0',{'request_key':'x'},'call')
        self.assertEqual(len(m['entities']),1)
        self.assertEqual(m['relations'],[])
        self.assertEqual(m['rejected_fields'][0]['kind'],'relation')

    def test_cuda_oom_stops_after_preserving_result_but_semantic_errors_do_not(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder);job=dict(id='job')
            stop_on_resource_error(out,job,dict(question_id='u',error=dict(message='Foreign source')),1)
            result=dict(question_id='u',error=dict(message='ToolFailure: OutOfMemoryError: CUDA out of memory'))
            write_json(out/'formal/result.json',result)
            with self.assertRaisesRegex(RuntimeError,'remaining questions not attempted'):
                stop_on_resource_error(out,job,result,1)
            self.assertEqual(json.loads((out/'formal/result.json').read_text()),result)
            self.assertFalse(json.loads((out/'workers/job/resource_stop.json').read_text())['automatic_retry'])

    def test_guard_stops_only_verified_worker_tree_and_refuses_reused_pid(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder);proc=out/'proc';pid=123;job=dict(id='job',arm='optimized_scheme4')
            process=proc/str(pid);process.mkdir(parents=True)
            command=['python',str(out/'source/script/run_followup_validation.py'),'worker','--out',str(out),'--job','job']
            (process/'cmdline').write_bytes(b'\0'.join(x.encode() for x in command)+b'\0')
            (process/'task/123').mkdir(parents=True)
            (process/'task/123/children').write_text('124')
            signals=[]
            self.assertTrue(stop_worker(out,job,dict(pid=pid),'u',proc_root=proc,kill=lambda *args:signals.append(args)))
            self.assertEqual([p for p,s in signals],[123,124])
            (process/'cmdline').write_bytes(b'python\0another-task.py\0')
            with self.assertRaisesRegex(ValueError,'identity changed'):
                stop_worker(out,job,dict(pid=pid),'u',proc_root=proc,kill=lambda *args:signals.append(args))
            self.assertEqual(len(signals),2)


if __name__ == '__main__':
    unittest.main()
