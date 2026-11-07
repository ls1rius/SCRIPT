import copy
from contextlib import contextmanager
import inspect
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from script import resident_q4_500 as r
from script import extend_q4_500 as ext
from script_core import budget

OUT = Path(__file__).resolve().parents[1]/'workdirs/Q4-adaptive-500-20261105-r1'


class ResidentTests(unittest.TestCase):
    def configs(self, p):
        for n in ['qa_config.json','source_config.json']:
            r.write(p/n,r.read(OUT/n))
        r.write(p/'claims.json',{})
        r.write(p/'cohort.json',['a','b'])

    def test_compatible_rejects_every_semantic_change(self):
        a=r.read(OUT/'qa_config.json');b=r.read(OUT/'source_config.json')
        r.compatible(a,b)
        for section,key,value in [('models','vlm','other'),('models','max_input_tokens',1),('budget','tool_call_cap',999),('query','caption_policy','other')]:
            c=copy.deepcopy(b);c[section][key]=value
            with self.assertRaises(ValueError):r.compatible(a,c)

    def test_single_process_single_lease_survive_many_stage_and_question_boundaries(self):
        events=[];instances=[]
        @contextmanager
        def acquire(policy,wait):
            events.append(('acquire',policy));yield dict(index='2',uuid='fake',reservation_id='lease'),()
            events.append(('release',))
        class Model:
            def __init__(self,cfg):self.cfg=cfg;self.process=SimpleNamespace(pid=123);instances.append(self)
            def call(self,op,inputs,timeout):events.append(('call',op));return dict(pid=123)
            def close(self,graceful=False):events.append(('close',))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.configs(p);folder=p/'workers/8';folder.mkdir(parents=True)
            session=r.ResidentSession(p,folder,lambda *a,**k:None,acquire,Model)
            for stage in ['q4','source','q4','source','q4']:
                with session.stage(stage) as (backend,cfg,gpu):
                    self.assertIs(backend,instances[0]);self.assertEqual(cfg,r.read(p/('qa_config.json' if stage=='q4' else 'source_config.json')))
                self.assertNotIn(('close',),events);self.assertNotIn(('release',),events)
            self.assertEqual(sum(e[0]=='acquire' for e in events),1)
            self.assertEqual(sum(e==('call','prepare_vlm') for e in events),1)
            self.assertEqual(sum(e==('call',r.CONTROL) for e in events),4)
            self.assertEqual(events[0][1]['minimum_free_mib'],32768)
            self.assertEqual(events[0][1]['preference'],['0','1','2','3'])
            session.close();self.assertEqual(events[-2:],[('close',),('release',)])

    def test_exception_closes_before_releasing_lease(self):
        events=[]
        @contextmanager
        def acquire(*a):
            yield dict(index='0',uuid='fake',reservation_id='lease'),()
            events.append('release')
        class Model:
            def __init__(self,cfg):self.cfg=cfg
            def call(self,*a):raise ValueError('load failed')
            def close(self,**k):events.append('close')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.configs(p);session=r.ResidentSession(p,p/'workers/8',lambda *a,**k:None,acquire,Model)
            try:
                with self.assertRaises(ValueError):
                    with session.stage('q4'):pass
            finally:session.close()
        self.assertEqual(events,['close','release'])

    def test_waiting_worker_drains_but_owned_source_is_not_abandoned(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.configs(p);r.write(p/'claims.json',dict(a=dict(state='complete',worker_id=1),b=dict(state='running',worker_id=8)))
            self.assertEqual(r.remaining(p),0);self.assertTrue(r.owned(p,8));self.assertFalse(r.owned(p,9))

    def test_resume_completed_source_without_reclaim_or_retry(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.configs(p);r.write(p/'claims.json',dict(a=dict(state='running',worker_id=8,index=1)))
            r.write(p/'source_runs/a/result.json',dict(answer='A'))
            self.assertEqual(r.resident_claim(p,8),(1,'a'))
            self.assertNotIn('b',r.read(p/'claims.json'))
            (p/'attempts/a').mkdir(parents=True)
            with self.assertRaises(ValueError):r.resident_claim(p,8)

    def test_no_resume_of_missing_source(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.configs(p);r.write(p/'claims.json',dict(a=dict(state='running',worker_id=8,index=1)))
            with self.assertRaises(ValueError):r.resident_claim(p,8)

    def test_new_claims_remain_frozen_atomic_claims(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);self.configs(p)
            self.assertEqual(r.resident_claim(p,8),(1,'a'))
            self.assertEqual(r.resident_claim(p,9),(2,'b'))
            r.write(p/'failure.json',{'message':'stop'})
            self.assertIsNone(r.resident_claim(p,10))

    def test_process_ipc_changes_config_and_preserves_loaded_model(self):
        code=r.process_code(inspect.getsource(budget._worker))
        class Backend:
            def __init__(self,cfg):
                self.cfg=cfg;self.loaded={'vlm':object()};self.gpu_acquired_at=None
            def call(self,op,inputs):
                return dict(data=[self.cfg['query']['field_extraction_batch_size'],id(self.loaded['vlm'])])
        qa=r.read(OUT/'qa_config.json');src=r.read(OUT/'source_config.json')
        class Connection:
            def __init__(self):
                self.messages=iter([('probe',{},0),(r.CONTROL,{'config':src},0),('probe',{},0),
                                    (r.CONTROL,{'config':qa},0),('probe',{},0),None]);self.sent=[]
            def recv(self):return next(self.messages)
            def send(self,v):self.sent.append(v)
        caps=[]
        cuda=SimpleNamespace(get_device_properties=lambda i:SimpleNamespace(total_memory=100*1024**3),
            set_per_process_memory_fraction=lambda n,i:caps.append(n),is_initialized=lambda:False)
        import sys
        with patch('script_core.backend.LocalModels',Backend),patch.dict(sys.modules,{'torch':SimpleNamespace(cuda=cuda),'resident_q4_500':r}):
            scope=dict(vars(budget));exec(compile(code,'fake-resident-process','exec'),scope)
            c=Connection();scope['_worker'](c,qa)
        values=[c.sent[i][1]['data'] for i in [0,2,4]]
        self.assertEqual([v[0] for v in values],[2,4,2])
        self.assertEqual(len({v[1] for v in values}),1)
        self.assertEqual(caps,[22528/102400,30720/102400,22528/102400])
        self.assertEqual(c.sent[1][1]['loaded'],['vlm'])

    def test_generated_worker_retains_original_model_calls_budgets_and_publication(self):
        original=inspect.getsource(ext.worker);changed=r.generated_worker(original)
        for line in original.splitlines():
            if any(x in line for x in ['result=answer(', 'result=q.run_case(', 'try:functional(', 'validate_source_batch(', 'validate_live(backend', 'source_binding.json', 'q.base.publish']):
                self.assertIn(line.strip(),changed)
        self.assertNotIn('backend.close(',changed)
        self.assertIn('finally:session.close()',changed)
        self.assertIn("if not (out/'source_runs'/uid).exists():",changed)
        self.assertIn('resident_claim(out,wid)',changed)

    def test_active_worker_handoff_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);r.write(p/'workers/0/status.json',{'stage':'query'})
            with patch.object(r,'identity',return_value='birth'),patch('script.gpu_shared_queue.process_snapshot',return_value={}),patch('script.gpu_shared_queue.allocations',return_value=[]):
                with self.assertRaisesRegex(ValueError,'active work'):
                    r.safe_idle_snapshot(p,[dict(id=0,pid=999,identity='birth')])


if __name__=='__main__':unittest.main()
