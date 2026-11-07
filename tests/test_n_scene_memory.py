from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script_core import adaptive_transition, partial_temporal, n_scene_memory as core
from script_core.config import load_config
from tests.test_m_scene_memory import Backend as MBackend, fixture


class Backend(MBackend):
    def n_source(self, inputs):
        data=self.source(inputs)
        from script_core import n_source_extraction as n
        schema=n.output_schema(n.EXTRACT,inputs)['properties']['actions']['items']
        props=schema.get('anyOf',[schema])[0]['properties']['roles']['properties']
        for action in data['actions']:
            values={r['role']:r['value'] for r in action['roles']}
            action['roles']={k:values.get(k) for k in props}
        return data

    def call(self, operation, inputs, *args):
        if operation == 'n_select_scene':
            self.calls.append((operation,deepcopy(inputs)))
            order=list(dict.fromkeys(self.candidate_order))
            return dict(data=dict(scene_reviews={s['id']:dict(option_reviews={
                f'O{i}':dict(anchor=next(iter(s['summary_clauses'].values())) if i==1 and s['id'] in order else None,
                    reason='Option-specific source review',
                    priority=100-order.index(s['id']) if i==1 and s['id'] in order else 0)
                for i in range(len(inputs['options'])) if f'O{i}'==inputs['focus_option']['option_id']}) for s in inputs['directory']}))
        if operation in ('n_extract_fields','n_extract_batch'):
            self.calls.append((operation,deepcopy(inputs)))
            if operation=='n_extract_batch':
                return dict(items=[dict(data=self.n_source(i)) for i in inputs['requests']])
            return dict(data=self.n_source(inputs))
        if operation in ('n_evidence_plan','n_answer'):
            result=super().call(operation.replace('n_','m_',1),inputs,*args)
            self.calls[-1]=(operation,deepcopy(inputs))
            return result
        return super().call(operation,inputs,*args)


class NSceneTests(unittest.TestCase):
    def run_case(self,root,backend):
        packet,directory=fixture();cfg=load_config('configs/script_N.yaml')
        cfg['query']['temporal_sparse_visual']=False
        search=adaptive_transition.run(packet,directory,[],backend,cfg,root/'search',
            dict(online_seconds=1500,max_scene_visits=4),evidence_only=True)
        self.assertIsNone(search['error'],search['error'])
        bundle=json.loads((root/'search/evidence_bundle.json').read_text())
        result=partial_temporal.run(packet,bundle,backend,cfg,root/'final',dict(online_seconds=1500))
        return result

    def test_cli_n_is_explicit_and_defaults_and_m_are_preserved(self):
        import script_qa
        seen=[]
        class Stop(Exception):pass
        def preflight(cfg):seen.append(cfg);raise Stop
        with patch.object(script_qa,'preflight',side_effect=preflight):
            for flags in (['--memory','N'],['--config','configs/script_N.yaml'],['--memory','M'],[]):
                with self.assertRaises(Stop):
                    script_qa.main(['query','--base','unused','--questions','unused','--out','unused']+flags)
        self.assertEqual([c['query']['temporal_qa_evidence'] for c in seen],
            [core.MODE,core.MODE,'scene_planned_script','planned_lazy_script'])

    def test_n_owns_calls_cache_artifacts_and_preserves_m_navigation_trace(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);b=Backend();r=self.run_case(root,b)
            self.assertIsNone(r['error'],r['error'])
            self.assertEqual(r['method'],core.VERSION)
            self.assertEqual(r['memory_variant'],'N')
            self.assertEqual(r['qa_evidence']['evidence_planner']['parent'],'M')
            self.assertIn('/N/I/h_script.sqlite3',r['qa_evidence']['database'])
            self.assertIn('N-required-role-review-v2',r['qa_evidence']['extractor_version'])
            ops=[op for op,_ in b.calls]
            self.assertIn('n_extract_fields',ops)
            self.assertEqual(ops.count('m_candidate_query'),1)
            self.assertEqual(ops.count('m_candidate_rank'),1)
            self.assertNotIn('n_select_scene',ops)
            self.assertNotIn('m_select_scene',ops)
            self.assertEqual(ops.count('n_evidence_plan'),1)
            self.assertEqual(ops.count('n_answer'),1)
            self.assertFalse(set(ops)&{'h_extract_fields','h_extract_batch','m_answer','m_evidence_plan'})
            trace=json.loads((root/'search/navigation_trace.json').read_text())
            self.assertEqual(trace['version'],core.VERSION)
            self.assertEqual(trace['opened_scenes'],['S3'])
            self.assertTrue(trace['events'])
            self.assertIn('scene_reviews',trace['candidate_selection'])
            self.assertTrue(any(e['event']=='selection_derived' for e in trace['events']))
            self.assertTrue((root/'final/N/final_input.json').is_file())
            self.assertFalse((root/'final/M').exists())
            self.assertEqual(json.loads((root/'final/entity_evidence_audit.json').read_text())['variant'],'N')
            for op,inp in b.calls:
                if op.startswith('n_extract'):
                    for request in inp.get('requests',[inp]):
                        self.assertNotIn('question',request)
                        self.assertNotIn('options',request)

    def test_empty_candidates_read_nothing_and_skip_extraction_and_planning(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);b=Backend(candidate_order=[]);r=self.run_case(root,b)
            self.assertIsNone(r['error'],r['error'])
            self.assertEqual(r['memory_variant'],'N')
            self.assertEqual(r['selected_seconds'],[])
            ops=[op for op,_ in b.calls]
            self.assertFalse(set(ops)&{'h_plan_fields','n_extract_fields','n_extract_batch','n_evidence_plan'})
            self.assertEqual(ops.count('n_answer'),1)
            saved=json.loads((root/'final/N/empty_evidence.json').read_text())
            self.assertEqual(saved['evidence_status'],'insufficient')

    def test_invalid_plan_is_saved_and_does_not_answer_or_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);b=Backend(invalid_planner=True);r=self.run_case(root,b)
            self.assertIsNotNone(r['error'])
            ops=[op for op,_ in b.calls]
            self.assertEqual(ops.count('n_evidence_plan'),1)
            self.assertNotIn('n_answer',ops)
            self.assertTrue((root/'final/N/planner_output.json').is_file())

    def test_partial_fact_keeps_original_without_resurrecting_all_skipped_sources(self):
        from script_core import merged_lazy_memory as merged
        from script.controlled_memory_io import parts
        payload=dict(observations=[
            dict(caption_id='C1',frame_index=1,sample_interval=[1,2],evidence=dict(
                original_text='C moves a brick on the brick wall with his left hand.',
                actions=[dict(actor='C',verb='moves',patient='brick')])),
            dict(caption_id='C2',frame_index=2,sample_interval=[2,3],evidence=dict(
                original_text='C washes a cup with a cloth.',
                actions=[dict(actor='C',verb='washes',patient='cup')]))])
        memory=merged.materialize(payload)
        qa=dict(head='test',prompts=['  Description: '+merged.render(memory)[0]+'\n\n###\n\n Questions: What happens?'])
        pool=core.build_pool(memory,qa,parts)
        kept=next(r['id'] for r in pool['records'] if r['text']=='C moves brick.')
        proposed=core.parent.decode_plan({r['id']:'keep' if r['id']==kept else 'skip' for r in pool['records']},pool)
        before=deepcopy(proposed)
        plan,overrides=core.preserve_incomplete_sources(pool,proposed)
        self.assertEqual(proposed,before)
        self.assertEqual(len(overrides),1)
        self.assertEqual(overrides[0]['caption_ids'],['C1'])
        rendered,_=core.render(pool,plan)
        self.assertIn('brick wall',rendered);self.assertIn('left hand',rendered)
        self.assertNotIn('cloth',rendered)
        self.assertEqual(core.preserve_incomplete_sources(pool,plan),(plan,[]))


if __name__=='__main__':unittest.main()
