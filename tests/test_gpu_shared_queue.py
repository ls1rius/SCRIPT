import unittest
from contextlib import contextmanager
from pathlib import Path
import os,tempfile
from unittest.mock import patch
from script import gpu_shared_queue as q,gpu_queue

class SharedQueueTests(unittest.TestCase):
    def test_total_capacity_ceiling_blocks_admission_with_stale_device_free(self):
        rows={'0':dict(uuid='card',free_mib=97250,total_mib=97887)}
        processes={i:dict(start=str(i),parent=0) for i in [1,2,3]}
        leases={str(i):dict(pid=i,start=str(i),uuid='card',capacity_mib=24576) for i in [1,2,3]}
        # Device free was sampled before cudaMalloc, process usage after it.
        usage=[dict(uuid='card',pid=3,mib=23198)]
        free,_=q.effective_free(rows,leases,processes,usage)
        self.assertLess(free['0'],24576)
        self.assertEqual(free['0'],97887-3*24576-1024)

    def test_total_capacity_ceiling_also_accounts_for_other_projects(self):
        rows={'0':dict(uuid='card',free_mib=60000,total_mib=97887)}
        processes={1:dict(start='1',parent=0),2:dict(start='2',parent=0)}
        leases={str(i):dict(pid=i,start=str(i),uuid='card',capacity_mib=24576) for i in [1,2]}
        usage=[dict(uuid='card',pid=1,mib=23198),dict(uuid='card',pid=2,mib=23198),dict(uuid='card',pid=99,mib=37206)]
        free,_=q.effective_free(rows,leases,processes,usage)
        self.assertEqual(free['0'],97887-2*24576-37206-1024)

    def test_live_reservation_counts_only_unallocated_capacity_and_cleans_dead(self):
        rows={'0':dict(uuid='card',free_mib=30000)}
        leases={'live':dict(pid=1,start='x',uuid='card',capacity_mib=24000),
                'dead':dict(pid=5,start='y',uuid='card',capacity_mib=90000)}
        processes={1:dict(start='x',parent=0),2:dict(start='z',parent=1),3:dict(start='z',parent=2)}
        free,active=q.effective_free(rows,leases,processes,[dict(uuid='card',pid=3,mib=20000)])
        self.assertEqual(free['0'],26000);self.assertEqual(set(active),{'live'})
        processes[1]['start']='new'
        self.assertEqual(q.effective_free(rows,leases,processes,[])[0]['0'],30000)

    def test_busy_gpu_is_admitted_and_second_task_cannot_spend_same_unallocated_memory(self):
        row=dict(index='0',uuid='card',free_mib=50000,utilization=100)
        processes={os.getpid():dict(start='self',parent=0)}
        with tempfile.TemporaryDirectory() as td,patch.object(q,'REGISTRY',Path(td)/'registry.json'), \
             patch.object(gpu_queue,'inventory',return_value={'0':row}),patch.object(q,'process_snapshot',return_value=processes), \
             patch.object(q,'allocations',return_value=[]),patch.object(q.time,'sleep'):
            policy=dict(preference=['0'],minimum_free_mib=24000,stable_observations=3)
            waits=[]
            with q.acquire(policy,waits.append) as (gpu,fds):
                self.assertEqual(gpu['utilization'],100);self.assertEqual(fds,());self.assertEqual(len(waits),2)
                # A second job can share this card, since another 26 GiB remain.
                with q.acquire(policy,waits.append) as (gpu2,_):self.assertEqual(gpu2['effective_free_mib'],26000)
                self.assertEqual(len(__import__('json').loads(q.REGISTRY.read_text())),1)
            self.assertEqual(q.REGISTRY.read_text(),'{}')

    def test_default_entry_uses_shared_admission_despite_legacy_low_util_limit(self):
        @contextmanager
        def shared(policy,wait):yield {'index':'2'},()
        with patch.object(q,'acquire',shared):
            with gpu_queue.acquire(dict(maximum_utilization=10),lambda x:None) as result:
                self.assertEqual(result,({'index':'2'},()))
