import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from script_core.physical_cuda_pool import validate_settings, resolved_models
from script.run_with_physical_cuda_pool import check_runtime


class SettingsTests(unittest.TestCase):
    def test_disabled_settings_need_no_cuda(self):
        self.assertEqual(validate_settings({'device':'cpu'}), 0)
        with patch.dict(os.environ, {'VIDEOAGENT_PHYSICAL_CUDA_POOL':'0'}):
            self.assertEqual(resolved_models({'device':'cuda'})['device'], 'cuda')

    def test_capacity_rejects_invalid_and_oversized_stage(self):
        for capacity in [-1, True, 1.5, '256']:
            with self.assertRaises(ValueError):
                validate_settings({'device':'cuda','cuda_physical_pool_mib':capacity})
        for fields in [{'device':'cpu'}, {'device':'cuda','cuda_allocator_limit_mib':257}]:
            with self.assertRaises(ValueError):
                validate_settings(dict(cuda_physical_pool_mib=256, **fields))
        self.assertEqual(validate_settings(dict(device='cuda',cuda_physical_pool_mib=256,cuda_allocator_limit_mib=0)), 256)

    def test_auto_capacity_requires_live_ancestor_lease_and_matches_gpu(self):
        from script import gpu_shared_queue as queue
        with tempfile.TemporaryDirectory() as d:
            registry=Path(d)/'registry.json'
            lease=dict(pid=100,start='birth',uuid='GPU-test',capacity_mib=43008)
            registry.write_text(json.dumps({'key':lease}))
            processes={os.getpid():dict(parent=100,start='child'),100:dict(parent=1,start='birth')}
            env={'VIDEOAGENT_PHYSICAL_CUDA_POOL':'1','VIDEOAGENT_GPU_LEASE':'key','CUDA_VISIBLE_DEVICES':'GPU-test'}
            with patch.object(queue,'REGISTRY',registry),patch.object(queue,'process_snapshot',return_value=processes),patch.dict(os.environ,env):
                for lease_capacity, pool_capacity in [(43008,40960),(32768,30720),(24576,22528)]:
                    lease['capacity_mib']=lease_capacity;registry.write_text(json.dumps({'key':lease}))
                    self.assertEqual(resolved_models(dict(device='cuda',cuda_allocator_limit_mib=22528))['cuda_physical_pool_mib'],pool_capacity)
                with self.assertRaises(ValueError):resolved_models(dict(device='cuda',cuda_physical_pool_mib=30000))
                for key,value in [('CUDA_VISIBLE_DEVICES','GPU-other'),('VIDEOAGENT_GPU_LEASE','unknown')]:
                    with patch.dict(os.environ,{key:value}):
                        with self.assertRaises(ValueError):resolved_models({'device':'cuda'})
                lease['start']='stale';registry.write_text(json.dumps({'key':lease}))
                with self.assertRaises(ValueError):resolved_models({'device':'cuda'})

    def test_launcher_refuses_old_runtime_without_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError,'NEW runtime'):check_runtime(Path(d))
            self.assertEqual(list(Path(d).iterdir()),[])

    def test_new_inherited_runtime_is_patched_before_freeze_only(self):
        import shutil
        from script.physical_pool_preparation import stage_support
        root=Path(__file__).resolve().parents[1]
        old=root/'workdirs/Q4-trained-fixed6-200train-300val-20261106-r1'
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);runtime=out/'runtime'
            shutil.copytree(old/'runtime',runtime,ignore=shutil.ignore_patterns('__pycache__'))
            control=out/'control/model_process.py';control.parent.mkdir()
            shutil.copy2(old/'control/model_process.py',control)
            before=(runtime/'script_core/q_v4.py').read_bytes()
            hashes=stage_support(runtime,[control])
            self.assertTrue(hashes)
            self.assertEqual(before,(runtime/'script_core/q_v4.py').read_bytes())
            self.assertIn('physical_cuda_pool import ensure',control.read_text())
            for path in [runtime/'script_core/budget.py',runtime/'script_core/backend.py',control]:
                compile(path.read_text(),str(path),'exec')
            self.assertTrue(Path(check_runtime(runtime)).is_file())
            for marker in ['launch.json','frozen_hashes.json']:
                (out/marker).write_text('{}')
                with self.assertRaisesRegex(ValueError,'new, unfrozen'):stage_support(runtime)
                (out/marker).unlink()


@unittest.skipUnless(os.environ.get('VIDEOAGENT_TEST_PHYSICAL_POOL_GPU') == '1', 'opt-in small GPU integration test')
class GPUIntegrationTests(unittest.TestCase):
    def test_real_capacity_streams_threads_training_limits_and_exit(self):
        # Parent must obtain shared admission before selecting CUDA_VISIBLE_DEVICES.
        code = r'''
import gc,json,threading,ctypes
from concurrent.futures import ThreadPoolExecutor
import torch
from script_core.physical_cuda_pool import install,status
assert not torch.cuda.is_initialized()
# Driver free-memory measurement independently verifies physical cudaMalloc.
from torch.utils.cpp_extension import CUDA_HOME
cudart=ctypes.CDLL(CUDA_HOME+'/lib64/libcudart.so')
cudart.cudaSetDevice(0);cudart.cudaFree(None)
def driver_free():
    free,total=ctypes.c_size_t(),ctypes.c_size_t()
    assert cudart.cudaMemGetInfo(ctypes.byref(free),ctypes.byref(total))==0
    return free.value
before=driver_free()
settings=dict(device='cuda',cuda_physical_pool_mib=256,cuda_allocator_limit_mib=256)
initial=install(settings)
physical_drop=before-driver_free()
assert physical_drop>=256*1024**2,physical_drop
assert initial['reserved_bytes']==256*1024**2, initial
assert torch.cuda.get_allocator_backend()=='videoagent_physical_pool'
# Forward/backward on another Python thread; all autograd allocations are global.
def train():
    x=torch.randn(128,128,device='cuda',requires_grad=True)
    optimizer=torch.optim.AdamW([x])
    for _ in range(3):
        optimizer.zero_grad();x.square().mean().backward();optimizer.step()
    assert torch.isfinite(x).all()
with ThreadPoolExecutor(2) as executor:
    list(executor.map(lambda _:train(),range(2)))
# The repository's actual LoRA layer, checkpointing and six optimizer steps.
from script_core.q4_retention_training import LowRankLinear
from torch.utils.checkpoint import checkpoint
def train_lora():
    layer=LowRankLinear(torch.nn.Linear(64,64,device='cuda',dtype=torch.bfloat16))
    layer.enabled=True
    optimizer=torch.optim.AdamW([layer.lora_A,layer.lora_B],lr=2e-5)
    for _ in range(6):
        optimizer.zero_grad(set_to_none=True)
        x=torch.randn(8,64,device='cuda',dtype=torch.bfloat16,requires_grad=True)
        checkpoint(layer,x,use_reentrant=False).float().square().mean().backward()
        assert layer.base.weight.grad is None
        assert layer.lora_B.grad is not None and torch.isfinite(layer.lora_B.grad).all()
        optimizer.step()
train_lora()
gc.collect();torch.cuda.synchronize()
# cuBLAS keeps two 32 MiB workspaces after matmuls; they are pool allocations.
baseline=torch.cuda.memory_allocated()
# Stream recording and rapid reuse must not corrupt asynchronous consumers.
s=torch.cuda.Stream();consume=torch.cuda.Stream()
for _ in range(30):
    with torch.cuda.stream(s):x=torch.full((1024*1024,),3.,device='cuda')
    consume.wait_stream(s)
    with torch.cuda.stream(consume):
        x.record_stream(consume);result=x.sum()
    del x
    with torch.cuda.stream(s):y=torch.zeros(1024*1024,device='cuda')
    with torch.cuda.stream(consume):
        observed=result.item()
    assert observed==3*1024*1024,observed
    del y,result
# Reuse and coalescence restore capacity despite many split allocations.
blocks=[torch.empty((i+1)*8192,device='cuda') for i in range(30)]
for i in range(0,30,2):blocks[i]=None
blocks=None;gc.collect();torch.cuda.synchronize();torch.cuda.empty_cache()
assert torch.cuda.memory_allocated()==baseline,torch.cuda.memory_stats()
held=status();assert held['reserved_bytes']==256*1024**2
large=torch.empty(160*1024**2,dtype=torch.uint8,device='cuda');del large
for cap in [192,256,128,256]:
    torch.cuda.set_per_process_memory_fraction(cap*1024**2/torch.cuda.get_device_properties(0).total_memory)
    assert torch.cuda.memory_reserved()==256*1024**2
try:torch.empty(257*1024**2,dtype=torch.uint8,device='cuda')
except torch.OutOfMemoryError as exc:assert 'no fallback' in str(exc)
else:raise AssertionError('capacity overrun accepted')
torch.cuda.reset_peak_memory_stats();assert torch.cuda.max_memory_allocated()==baseline
assert install(settings)['capacity_mib']==256
try:install(dict(settings,cuda_physical_pool_mib=128))
except ValueError:pass
else:raise AssertionError('live resize accepted')
import os
print(json.dumps(dict(passed=True,model_pid=os.getpid(),physical_driver_drop_bytes=physical_drop,initial=initial,held=held,final=status())))
'''
        env=dict(os.environ,VIDEOAGENT_PHYSICAL_CUDA_POOL='0')
        p=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        result=json.loads(p.stdout.splitlines()[-1])
        from script.gpu_shared_queue import allocations
        self.assertFalse(any(row['pid']==result['model_pid'] for row in allocations()))
        print(p.stdout.strip())


if __name__=='__main__':unittest.main()
