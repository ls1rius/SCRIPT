from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from script import watch_mnop_alerts as monitor


class AlertTests(unittest.TestCase):
    def test_controlled_handoff_is_not_a_failure_but_real_failure_still_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            monitor.write(p/'queue_status.json',dict(state='failed',controller_pid=123,
                workers={'0':dict(stage='complete')}))
            monitor.write(p/'policy_handoff.json',dict(reason='user changed evidence policy'))
            monitor.write(p/'report.json',dict(errors=0,scheduler_failure=None,complete=False))
            inspected=monitor.inspect_run('Q',p,lambda _:False)
            self.assertEqual(inspected['state'],'superseded');self.assertTrue(inspected['terminal'])
            self.assertIsNone(inspected['event'])
            monitor.write(p/'failure.json',dict(message='real model error'))
            self.assertEqual(monitor.inspect_run('Q',p,lambda _:False)['event']['reason'],'real model error')

    def test_waiting_is_not_failure_and_complete_does_not_need_process(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            monitor.write(p/'queue_status.json',dict(state='waiting_for_N_source',controller_pid=123))
            self.assertIsNone(monitor.inspect_run('O',p,lambda _:True)['event'])
            monitor.write(p/'queue_status.json',dict(state='complete',controller_pid=123))
            self.assertIsNone(monitor.inspect_run('O',p,lambda _:False)['event'])

    def test_parallel_failure_detected_before_other_worker_finishes(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            monitor.write(p/'queue_status.json',dict(state='running',controller_pid=123,scheduler=str(p/'control')))
            monitor.write(p/'control/failure.json',dict(message='deadline',at='fixed'))
            event=monitor.inspect_run('N',p,lambda _:True)['event']
            self.assertEqual(event['reason'],'deadline')
            self.assertFalse(event['confirmation_required'])

    def test_auxiliary_exit_is_detected_without_claiming_model_failed(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            monitor.write(p/'queue_status.json',dict(state='running',controller_pid=123,scheduler=str(p/'control')))
            monitor.write(p/'control/origin_aliases_launch.json',dict(pid=456))
            event=monitor.inspect_run('N',p,lambda pid:pid==123)['event']
            self.assertTrue(event['confirmation_required'])
            self.assertIn('路径维护进程',event['reason'])
            self.assertIsNone(monitor.inspect_run('N',p,lambda _:True)['event'])

    def test_auxiliary_normal_exit_after_completion_is_not_failure(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            monitor.write(p/'queue_status.json',dict(state='complete',controller_pid=123,scheduler=str(p/'control')))
            monitor.write(p/'control/origin_aliases_launch.json',dict(pid=456))
            self.assertIsNone(monitor.inspect_run('N',p,lambda _:False)['event'])

    def test_dead_controller_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);monitor.write(p/'queue_status.json',dict(state='running',controller_pid=123))
            state=dict(notified={},observations={},last_attempt={},deliveries=[])
            config=dict(runs={'M':str(p)});sent=[]
            def send(event):sent.append(event);return dict(queued=True)
            with patch.object(monitor,'alive',return_value=False):
                # Default function binding is explicit so this fixture doesn't depend on live PIDs.
                original=monitor.inspect_run
                with patch.object(monitor,'inspect_run',side_effect=lambda n,p:original(n,p,lambda _:False)):
                    monitor.scan(config,state,send);self.assertFalse(sent)
                    monitor.scan(config,state,send);self.assertEqual(len(sent),1)
                    monitor.scan(config,state,send);self.assertEqual(len(sent),1)

    def test_failed_status_queues_once_despite_new_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);state=dict(notified={},observations={},last_attempt={},deliveries=[])
            config=dict(runs={'P':str(p)});sent=[]
            def send(event):sent.append(event);return dict(queued=True)
            monitor.write(p/'queue_status.json',dict(state='failed',controller_pid=123,error='duplicate',updated_at='one'))
            self.assertTrue(monitor.scan(config,state,send))
            monitor.write(p/'queue_status.json',dict(state='failed',controller_pid=123,error='duplicate',updated_at='two'))
            monitor.scan(config,state,send);self.assertEqual(len(sent),1)

    def test_failed_delivery_is_not_claimed_delivered(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);monitor.write(p/'queue_status.json',dict(state='failed',error='broken'))
            state=dict(notified={},observations={},last_attempt={},deliveries=[])
            self.assertFalse(monitor.scan(dict(runs={'P':str(p)}),state,lambda _:dict(queued=False)))
            self.assertEqual(state['notified'],{})
            self.assertFalse(state['deliveries'][0]['result']['queued'])


if __name__=='__main__':unittest.main()
