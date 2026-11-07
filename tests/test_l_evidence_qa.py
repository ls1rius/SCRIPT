from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script import controlled_memory_io as io
from script_core import l_evidence_qa as core, partial_temporal
from script_core.config import load_config
from tests.test_entity_evidence_qa import fixture
from tests.test_merged_lazy_memory import Backend as HBackend
from tests.test_m_presentation import pool_fixture


class Backend(HBackend):
    def __init__(self, bad=None):
        super().__init__()
        self.bad=bad
    def call(self, operation, inputs, *args):
        if not operation.startswith('l_'):
            return super().call(operation,inputs,*args)
        self.calls.append((operation,deepcopy(inputs)))
        if operation=='l_evidence_plan':
            if self.bad=='exception':raise RuntimeError('planner failure')
            if self.bad=='duplicate':return dict(raw='{"R0":"keep","R0":"skip"}')
            task=json.loads(inputs['inputs']['prompts'][0].split('\n',1)[1])
            records=task['evidence']
            plan={r['id']:'skip' if self.bad=='empty' or 'wet' in r['text'] else 'keep' for r in records}
            if self.bad=='missing':plan.pop(next(iter(plan)))
            return dict(raw=json.dumps(plan))
        if operation=='l_answer':return dict(data=dict(selected_option='0',raw='prediction: A'))
        raise AssertionError(operation)


class LOnlineTests(unittest.TestCase):
    def test_default_cli_and_profiles_select_l_before_preflight(self):
        import script_qa
        class Stop(Exception):pass
        configs=[]
        def preflight(cfg):configs.append(cfg);raise Stop
        with patch.object(script_qa,'preflight',side_effect=preflight):
            for flags in ([],['--memory','L'],['--config','configs/script_local.yaml'],['--config','configs/script_L.yaml']):
                with self.subTest(flags=flags), self.assertRaises(Stop):
                    script_qa.main(['query','--base','unused','--questions','unused','--out','unused']+flags)
        for cfg in configs:
            self.assertEqual(cfg['query']['temporal_qa_evidence'],core.MODE)
            self.assertEqual(cfg['query']['json_repair_retries'],0)
        self.assertEqual(load_config()['query']['temporal_qa_evidence'],core.MODE)

    def test_default_query_executes_h_i_then_one_l_plan_and_one_l_answer(self):
        packet,bundle=fixture();before=deepcopy(bundle)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);backend=Backend()
            result=partial_temporal.run(packet,bundle,backend,load_config(),root,dict(online_seconds=1500))
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['memory_variant'],'L')
            self.assertEqual(result['qa_evidence']['mode'],core.MODE)
            self.assertEqual(result['qa_evidence']['parent_memory_variant'],'I')
            self.assertIn('/L/I/h_script.sqlite3',result['qa_evidence']['database'])
            ops=[op for op,_ in backend.calls]
            self.assertIn('h_plan_fields',ops)
            self.assertEqual(ops.count('l_evidence_plan'),1)
            self.assertEqual(ops.count('l_answer'),1)
            self.assertFalse(set(ops)&{'m_select_scene','m_transition_next','m_answer','videotree_qa'})
            qa=next(inp['inputs'] for op,inp in backend.calls if op=='l_answer')
            self.assertIn('Frames [0,1):',io.parts(qa)[1])
            self.assertIn('left hand',io.parts(qa)[1])
            self.assertNotIn('(source assertion)',io.parts(qa)[1])
            self.assertNotIn('wet',qa['prompts'][0])
            self.assertNotIn('Unread secret',qa['prompts'][0])
            self.assertEqual(io.read(root/'L/final_input.json'),qa)
            pool=io.read(root/'L/pool.json')
            self.assertEqual(pool['parent_memory_variant'],'I')
            self.assertEqual(len(pool['source_memory']['observations']),3)
            for op,inp in backend.calls:
                if op.startswith('h_extract'):
                    for request in inp.get('requests',[inp]):
                        self.assertNotIn('question',request);self.assertNotIn('options',request)
            self.assertIn('l_evidence_plan',result['cost_summary']['by_operation'])
            self.assertEqual(bundle,before)

    def test_failed_or_invalid_plan_does_not_answer_retry_or_fallback(self):
        for bad in ['empty','missing','duplicate','exception']:
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as td:
                packet,bundle=fixture();backend=Backend(bad)
                result=partial_temporal.run(packet,bundle,backend,load_config(),td,dict(online_seconds=1500))
                self.assertIsNotNone(result['error'])
                ops=[op for op,_ in backend.calls]
                self.assertEqual(ops.count('l_evidence_plan'),1)
                self.assertFalse(set(ops)&{'l_answer','videotree_qa','m_answer'})
                if bad!='exception':self.assertTrue((Path(td)/'L/planner_output.json').exists())

    def test_compact_presentation_preserves_l_sources_and_adjacent_ranges(self):
        pool=pool_fixture();plan=core.core.keep_all(pool)
        old,old_audit=core.core.render(pool,plan);new,new_audit=core.render(pool,plan)
        self.assertIn('Frames [4,6), [15,17): C walks dog.',new)
        self.assertIn('(text-inferred hypothesis)',new)
        self.assertNotIn('(source assertion)',new)
        for key in old_audit:self.assertEqual(new_audit[key],old_audit[key])

    def test_l_can_run_through_ordinary_query_dispatch(self):
        from script_core.scheme4 import answer
        from script_core.budget import BudgetLedger
        from tests.test_m_scene_memory import fixture as scene_fixture
        packet,directory=scene_fixture();packet.update(feature_hash='features',frames=[])
        directory['cost_summary']=BudgetLedger(600).summary()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);io.write(root/'directory.json',directory)
            backend=Backend();cfg=load_config();cfg['query']['temporal_sparse_visual']=False
            base=backend.call
            def call(op,inp,*args):
                if op=='scene_scope':return dict(data=dict(scope='local',reason='bounded action'))
                if op=='clip_text':return dict(vectors=[[1.,0.]]*6)
                if op=='transition_next':return dict(data=dict(sufficient=True,reason='text sufficient',gap='',
                    action='stop',scene_id='stop',caption_ids=[],entity_ids=[],focus=[],fields=[]))
                return base(op,inp,*args)
            backend.call=call
            result=answer(packet,[],cfg,backend,root)
            self.assertIsNone(result['error'],result['error'])
            self.assertEqual(result['memory_variant'],'L')
            self.assertTrue((root/'partial_search/result.json').exists())
            self.assertTrue((root/'partial_temporal/L/final_input.json').exists())


if __name__=='__main__':unittest.main()
