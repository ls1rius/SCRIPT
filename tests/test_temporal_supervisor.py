"""PBS outage and restart regressions; no GPU or scheduler mutations."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from script import supervise_temporal_suites as supervisor


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def write(self,path,value):
        supervisor.write(path,value)

    def suites(self):
        roots=[self.root/'temporal',self.root/'aligned']
        for i,root in enumerate(roots):
            self.write(root/'experiment.json',dict(shards=1))
            self.write(root/'jobs.json',dict(verification=f'check{i}',formal=[]))
            self.write(root/'source_hashes.json',{})
            self.write(root/'control_hashes.json',{})
            self.write(root/'verification/status.json',dict(passed=True))
        return roots

    def task(self):
        task=dict(root=str(self.root/'temporal'),suite='temporal',worker='temporal_cross',group=0,job_id='1')
        self.write(Path(task['root'])/'shards/0.json',['question'])
        return task

    def result(self,task,result):
        self.write(Path(task['root'])/'arms/temporal_cross/runs/question/result.json',result)

    def test_connection_timeout_and_malformed_response_are_retryable(self):
        outcomes=[Mock(returncode=1,stderr='Connection refused'),
                  subprocess.TimeoutExpired('qstat',40),
                  Mock(returncode=0,stdout='truncated'),
                  Mock(returncode=0,stdout='{"Jobs":{}}')]
        with patch.object(supervisor.subprocess,'run',side_effect=outcomes):
            for _ in outcomes:
                with self.assertRaises(supervisor.PBSUnavailable):supervisor.query(['job'])

    def test_outage_preserves_active_jobs_and_recovers_with_backoff(self):
        state=dict(phase='formal',active=[dict(job_id='existing')])
        observations=[]
        def sleep(delay):
            observations.append((delay,json.loads((self.root/'status.json').read_text())))
        with patch.object(supervisor,'query',side_effect=[supervisor.PBSUnavailable('refused')]*6+[{'existing':{'job_state':'R'}}]), \
             patch.object(supervisor.time,'sleep',side_effect=sleep):
            result=supervisor.query_reliably(['existing'],state,self.root)
        self.assertEqual(result['existing']['job_state'],'R')
        self.assertEqual([x[0] for x in observations],[5,10,20,40,60,60])
        self.assertTrue(all(s['phase']=='waiting_for_pbs' and s['active']==[dict(job_id='existing')] for _,s in observations))
        self.assertEqual(state['phase'],'formal')
        self.assertEqual(state['pbs_consecutive_failures'],0)
        self.assertEqual(state['pbs_failures'],6)

    def test_restart_restores_both_submitted_jobs_before_pending_work(self):
        temporal,aligned=self.suites()
        self.write(temporal/'jobs.json',dict(formal=[dict(arm='temporal_cross',group=0,job_id='old1')]))
        self.write(aligned/'jobs.json',dict(formal=[dict(worker='paired',group=0,job_id='old2')]))
        pending,active=supervisor.recover_tasks(supervisor.tasks(temporal,aligned))
        self.assertEqual([t['job_id'] for t in active],['old1','old2'])
        self.assertEqual(len(pending),3)
        self.assertNotIn('paired',[t['worker'] for t in pending])

    def test_duplicate_manifest_is_rejected(self):
        temporal,aligned=self.suites()
        entry=dict(arm='temporal_cross',group=0,job_id='old1')
        self.write(temporal/'jobs.json',dict(formal=[entry,entry]))
        with self.assertRaises(ValueError):supervisor.recover_tasks(supervisor.tasks(temporal,aligned))

    def test_temporal_only_plan_does_not_submit_image_based_aligned_suite(self):
        temporal,_=self.suites()
        planned=supervisor.tasks(temporal)
        self.assertEqual(len(planned),3)
        self.assertTrue(all(t['suite']=='temporal' for t in planned))

    def test_four_arm_plan_pairs_deep_and_cross_without_duplicate_workers(self):
        temporal,_=self.suites()
        self.write(temporal/'experiment.json',dict(shards=1,arms={'cluster_qa':'','temporal_local':'','summary_deep':'','temporal_cross':''}))
        planned=supervisor.tasks(temporal)
        self.assertEqual([t['worker'] for t in planned],['summary_pair','temporal_local','cluster_qa'])
        self.assertNotIn('temporal_cross',[t['worker'] for t in planned])

    def test_paired_worker_inspects_both_deep_and_cross_outputs(self):
        task=self.task();task['worker']='summary_pair'
        self.result(dict(task,worker='temporal_cross'),dict(answer=dict(selected_option='0')))
        issues=supervisor.inspect(task)
        self.assertEqual(len(issues),1)
        self.assertEqual(issues[0]['arm'],'summary_deep')

    def test_query_error_is_retained_once_and_result_is_unchanged(self):
        task=self.task();result=dict(error=dict(code='temporal_pipeline_failed'),answer=None)
        self.result(task,result);state=dict(query_errors=[])
        supervisor.check_task(task,state,True,True)
        supervisor.check_task(task,state,True,True)
        self.assertEqual(len(state['query_errors']),1)
        path=Path(task['root'])/'arms/temporal_cross/runs/question/result.json'
        self.assertEqual(supervisor.read(path),result)

    def test_query_error_stops_without_opt_in(self):
        task=self.task();self.result(task,dict(error='failed',answer=None))
        with self.assertRaises(RuntimeError):supervisor.check_task(task,dict(query_errors=[]),True,False)

    def test_missing_result_and_invalid_answer_stop_even_with_opt_in(self):
        task=self.task()
        with self.assertRaises(RuntimeError):supervisor.check_task(task,dict(query_errors=[]),True,True)
        self.result(task,dict(answer=dict(selected_option='9')))
        with self.assertRaises(RuntimeError):supervisor.check_task(task,dict(query_errors=[]),True,True)

    def test_budget_violation_is_not_hidden_by_query_error(self):
        task=self.task()
        self.result(task,dict(error='failed',cost_summary=dict(budget_violation=True)))
        with self.assertRaises(RuntimeError):supervisor.check_task(task,dict(query_errors=[]),True,True)

    def test_restart_tracks_running_job_and_only_fills_available_slot(self):
        temporal,aligned=self.suites()
        self.write(temporal/'jobs.json',dict(verification='check0',formal=[dict(arm='temporal_cross',group=0,job_id='old1')]))
        self.write(aligned/'jobs.json',dict(verification='check1',formal=[dict(worker='paired',group=0,job_id='old2')]))
        polled=[];submitted=[]
        def query(ids):
            polled.append(list(ids))
            if ids==['old1','old2']:
                return dict(old1=dict(job_state='F',Exit_status=0),old2=dict(job_state='R'))
            return {jid:dict(job_state='F',Exit_status=0) for jid in ids}
        def submit(task):
            submitted.append(task['worker'])
            return 'new'+str(len(submitted))
        with patch.object(supervisor,'query',side_effect=query), \
             patch.object(supervisor,'submit',side_effect=submit), \
             patch.object(supervisor,'inspect',return_value=[]), \
             patch.object(supervisor.time,'sleep'), \
             patch.object(supervisor.subprocess,'run',return_value=Mock(returncode=0,stdout='',stderr='')) as run:
            supervisor.main(temporal,aligned,self.root/'state',True)
        self.assertEqual(polled[1],['old1','old2'])
        self.assertEqual(polled[2],['old2','new1'])
        self.assertTrue(all(len(ids)<=2 for ids in polled))
        self.assertEqual(submitted,['temporal_local','videotree','cluster_qa'])
        state=supervisor.read(self.root/'state/status.json')
        self.assertEqual(state['phase'],'complete')
        self.assertEqual(len(state['completed']),5)
        self.assertFalse(any(call.args[0][0]=='qdel' for call in run.call_args_list))


if __name__=='__main__':unittest.main()
