"""Add allocator support to a NEW inherited runtime, before its hashes freeze."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def patch_worker(code):
    if 'physical_cuda_pool import ensure' in code:
        return code
    anchor = "            if not allocator_limit_applied and cfg['models'].get('cuda_allocator_limit_mib'):\n"
    if code.count(anchor) != 1:
        raise ValueError('Unknown model worker layout; cannot safely add physical pool support')
    code = code.replace(anchor,
        "            from script_core.physical_cuda_pool import ensure, status as pool_status\n"
        "            ensure(cfg['models'])\n" + anchor, 1)
    anchor = "            usage['gpu_peak_allocated_bytes'] = torch.cuda.max_memory_allocated() if gpu else 0\n"
    if code.count(anchor) != 1:
        raise ValueError('Unknown worker usage layout')
    return code.replace(anchor, anchor +
        "            if pool_status() is not None:\n"
        "                usage['cuda_physical_pool'] = pool_status()\n", 1)


def stage_support(runtime, controls=()):
    runtime = Path(runtime)
    out = runtime.parent
    if any((out/name).exists() for name in ['launch.json','frozen_hashes.json']):
        raise ValueError('Physical pool support must be staged in a new, unfrozen experiment')
    changed = []
    def edit(path, transform):
        original = path.read_text()
        result = transform(original)
        if result != original:
            path.write_text(result)
            changed.append(str(path.relative_to(out)))
    edit(runtime/'script_core/budget.py', patch_worker)
    def backend(code):
        if 'physical_cuda_pool import ensure' in code:
            return code
        anchor = "        import torch\n        from transformers import (AutoModel, AutoProcessor, AutoTokenizer, CLIPImageProcessor,\n"
        if code.count(anchor) != 1:
            raise ValueError('Unknown local model loading layout')
        return code.replace(anchor,
            "        from .physical_cuda_pool import ensure\n        ensure(self.cfg['models'])\n" + anchor, 1)
    edit(runtime/'script_core/backend.py', backend)
    trainer = runtime/'script/train_q4_retention.py'
    if trainer.exists():
        def training(code):
            if 'physical_cuda_pool import ensure' in code:
                return code
            anchor = "    cfg['models']['cuda_allocator_limit_mib']=40960\n"
            if code.count(anchor) != 1:
                raise ValueError('Unknown direct training loader layout')
            return code.replace(anchor, anchor +
                "    from script_core.physical_cuda_pool import ensure\n    ensure(cfg['models'])\n", 1)
        edit(trainer, training)
    for control in controls:
        edit(Path(control), patch_worker)
    for name in ['script_core/physical_cuda_pool.py', 'script_core/csrc/physical_cuda_pool.cpp',
                 'script/physical_pool_preparation.py','script/run_with_physical_cuda_pool.py']:
        target = runtime/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/name,target)
        changed.append(str(target.relative_to(out)))
    hashes = {name:hashlib.sha256((out/name).read_bytes()).hexdigest() for name in changed}
    (runtime/'physical_cuda_pool_support.json').write_text(json.dumps(dict(
        changed_files=hashes, physical_allocation_performed=False,
        activation='VIDEOAGENT_PHYSICAL_CUDA_POOL=1',
        policy='shared lease minus2048MiB; one fixed arena until worker process exit'),indent=2)+'\n')
    return hashes
