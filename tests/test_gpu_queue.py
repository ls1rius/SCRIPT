from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script import gpu_queue as queue


def inventory(free_indices=(), busy_indices=()):
    return {str(i): dict(index=str(i), uuid=f'test-{i}',
        free_mib=40000 if i in free_indices else 10000,
        utilization=100 if i in busy_indices else 0) for i in range(4)}


class GPUQueueTests(unittest.TestCase):
    def test_memory_rank_selects_any_card_instead_of_always_gpu_zero(self):
        policy=dict(preference=['0','1','2','3'], minimum_free_mib=32768,
            maximum_utilization=100, stable_observations=3, selection='most_free_memory')
        sample=inventory((0,1,3),(0,1,3))
        sample['0']['free_mib']=50000
        sample['1']['free_mib']=84000
        sample['3']['free_mib']=84000
        sample['1']['utilization']=98
        sample['3']['utilization']=96
        waits=[]
        with tempfile.TemporaryDirectory() as td, patch.object(queue,'Path',return_value=Path(td)), \
                patch.object(queue,'inventory',return_value=sample),patch.object(queue.time,'sleep'):
            with queue.acquire_exclusive(policy,waits.append) as (gpu,fds):
                self.assertEqual(gpu['index'],'3')
                self.assertEqual(len(waits),2)
                self.assertEqual(waits[-1]['stable_observations'],{'0':2,'1':2,'2':0,'3':2})

    def run_case(self, samples, expected, blocked_uuid=None):
        waits=[]
        def lock(handle, flags):
            if blocked_uuid and blocked_uuid in str(handle.name):
                raise BlockingIOError()
        with tempfile.TemporaryDirectory() as td, \
                patch.object(queue, 'Path', return_value=Path(td)), \
                patch.object(queue, 'inventory', side_effect=samples), \
                patch.object(queue.time, 'sleep'), \
                patch.object(queue.fcntl, 'flock', side_effect=lock):
            with queue.acquire_exclusive(dict(gpu_index='0,1,2,3', stable_observations=3), waits.append) as (gpu, fds):
                self.assertEqual(gpu['index'], expected)
                self.assertEqual(len(fds), 2)
        return waits

    def test_selects_any_of_four_cards_and_rejects_busy_card_with_free_memory(self):
        sample=inventory((0,3),(0,))
        waits=self.run_case([sample]*4,'3')
        self.assertEqual(len(waits),2)
        self.assertEqual(waits[0]['watched_gpu_indices'],['0','1','2','3'])
        self.assertEqual(waits[-1]['stable_observations']['0'],0)

    def test_stability_is_per_card_not_accumulated_across_cards(self):
        waits=self.run_case([inventory((0,)),inventory((1,)),inventory((1,)),inventory((1,)),inventory((1,))],'1')
        self.assertEqual(len(waits),3)

    def test_locked_card_is_skipped_without_failing_queue(self):
        self.run_case([inventory((0,2))]*4,'2','test-0')

    def test_memory_is_rechecked_after_lock_before_launch(self):
        waits=self.run_case([inventory((0,))]*3+[inventory(())]+[inventory((2,))]*4,'2')
        self.assertEqual(len(waits),5)

    def test_memory_only_policy_accepts_busy_gpu_but_still_checks_capacity(self):
        policy=dict(minimum_free_mib=32768,maximum_utilization=100,stable_observations=3)
        sample=inventory((0,),(0,1,2,3))
        self.assertTrue(queue.eligible(sample['0'],policy))
        self.assertFalse(queue.eligible(sample['1'],policy))
        waits=[]
        with tempfile.TemporaryDirectory() as td, \
                patch.object(queue,'Path',return_value=Path(td)), \
                patch.object(queue,'inventory',return_value=sample), \
                patch.object(queue.time,'sleep'):
            with queue.acquire_exclusive(policy,waits.append) as (gpu,fds):
                self.assertEqual(gpu['index'],'0')
                self.assertEqual(gpu['utilization'],100)
                self.assertEqual(len(waits),2)
                self.assertEqual(len(fds),2)


if __name__=='__main__':unittest.main()
