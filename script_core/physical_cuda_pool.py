"""Opt-in, process-wide physical CUDA capacity, held until worker exit.

Install before CUDA initialization. PyTorch tensors (including autograd threads)
suballocate the same fixed arena. empty_cache and stage changes retain capacity.
CUDA context/library allocations still require admission headroom outside it.
"""
import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess

_LIBRARY = None
_CAPACITY_MIB = None


def resolved_models(models):
    """Auto mode uses the existing shared lease minus 2 GiB CUDA headroom."""
    result = dict(models)
    mode = os.environ.get('VIDEOAGENT_PHYSICAL_CUDA_POOL', '0')
    if mode not in ('0', '1'):
        raise ValueError('VIDEOAGENT_PHYSICAL_CUDA_POOL must be 0 or 1')
    if mode == '1' and models.get('device') == 'cuda':
        from script.gpu_shared_queue import REGISTRY, process_snapshot
        import json
        key = os.environ.get('VIDEOAGENT_GPU_LEASE')
        leases = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}
        lease = leases.get(key)
        processes = process_snapshot()
        ancestors, pid = set(), os.getpid()
        while pid in processes and pid not in ancestors:
            ancestors.add(pid)
            pid = processes[pid]['parent']
        if (not lease or lease['pid'] not in ancestors or
                processes.get(lease['pid'], {}).get('start') != lease['start'] or
                os.environ.get('CUDA_VISIBLE_DEVICES') != lease['uuid']):
            raise ValueError('Physical CUDA pool requires a live ancestor GPU lease on the selected GPU')
        capacity = int(lease['capacity_mib']) - 2048
        requested = result.get('cuda_physical_pool_mib', 0)
        if requested and requested > capacity:
            raise ValueError('Physical CUDA pool exceeds lease minus 2048 MiB headroom')
        result['cuda_physical_pool_mib'] = requested or capacity
    validate_settings(result)
    return result


def ensure(models):
    return install(resolved_models(models))


def validate_settings(models):
    capacity = models.get('cuda_physical_pool_mib', 0)
    if type(capacity) is not int or capacity < 0:
        raise ValueError('cuda_physical_pool_mib must be a nonnegative integer')
    if capacity:
        if models.get('device') != 'cuda':
            raise ValueError('Physical CUDA pool requires models.device=cuda')
        limit = models.get('cuda_allocator_limit_mib') or capacity
        if type(limit) is not int or not 0 < limit <= capacity:
            raise ValueError('CUDA allocator limit must fit physical pool capacity')
    return capacity


def build_library():
    """Build against the installed torch ABI; serialized, content-addressed cache."""
    import torch
    from torch.utils.cpp_extension import CUDA_HOME
    if CUDA_HOME is None:
        raise RuntimeError('Physical CUDA pool requires the CUDA development toolkit')
    source = Path(__file__).with_name('csrc') / 'physical_cuda_pool.cpp'
    torch_root = Path(torch.__file__).parent
    key = hashlib.sha256(source.read_bytes() + str((torch.__version__, torch.version.cuda,
        torch._C._GLIBCXX_USE_CXX11_ABI, str(torch_root), CUDA_HOME)).encode()).hexdigest()[:20]
    cache = Path(os.environ.get('VIDEOAGENT_CUDA_POOL_CACHE',
                                str(Path.home()/'.cache/videoagent/cuda_pool')))
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (key + '.so')
    with (cache / (key + '.lock')).open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not target.exists():
            temporary = target.with_suffix(f'.{os.getpid()}.tmp.so')
            command = ['g++', '-std=c++17', '-O2', '-shared', '-fPIC', '-pthread',
                '-D_GLIBCXX_USE_CXX11_ABI=' + str(int(torch._C._GLIBCXX_USE_CXX11_ABI)),
                '-I'+str(torch_root/'include'), '-I'+str(Path(CUDA_HOME)/'include'),
                str(source), '-L'+str(torch_root/'lib'), '-Wl,-rpath,'+str(torch_root/'lib'),
                '-L'+str(Path(CUDA_HOME)/'lib64'), '-Wl,-rpath,'+str(Path(CUDA_HOME)/'lib64'),
                '-ltorch_cuda', '-lc10_cuda', '-lc10', '-ltorch_cpu', '-lcudart', '-o', str(temporary)]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
                temporary.replace(target)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError('Physical CUDA pool build failed:\n'+exc.stderr) from exc
            finally:
                temporary.unlink(missing_ok=True)
    return target


def install(models):
    global _LIBRARY, _CAPACITY_MIB
    capacity = validate_settings(models)
    if not capacity:
        if _CAPACITY_MIB is not None:
            raise ValueError('Cannot disable a resident physical CUDA pool')
        return None
    if _CAPACITY_MIB is not None:
        if capacity != _CAPACITY_MIB:
            raise ValueError('Cannot resize a resident physical CUDA pool')
        return status()
    import torch
    if torch.cuda.is_initialized():
        raise RuntimeError('Install physical CUDA pool before CUDA initialization')
    library = ctypes.CDLL(str(build_library()))
    library.videoagent_install_pool.argtypes = [ctypes.c_size_t, ctypes.c_int]
    library.videoagent_install_pool.restype = ctypes.c_char_p
    error = library.videoagent_install_pool(capacity * 1024**2, 0)
    if error:
        raise RuntimeError(error.decode())
    _LIBRARY, _CAPACITY_MIB = library, capacity
    torch.cuda.init()
    return status()


def status():
    if _CAPACITY_MIB is None:
        return None
    import torch
    return dict(capacity_mib=_CAPACITY_MIB,
                reserved_bytes=torch.cuda.memory_reserved(),
                live_bytes=torch.cuda.memory_allocated(),
                peak_live_bytes=torch.cuda.max_memory_allocated(),
                release_policy='worker_process_exit')
