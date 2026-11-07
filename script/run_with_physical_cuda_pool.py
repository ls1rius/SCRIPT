"""Launch a newly prepared experiment with lease-sized physical CUDA pools.

This launcher requires the updated working-tree runtime to be frozen into the
new experiment. It deliberately refuses to patch an existing frozen runtime.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check_runtime(root):
    core = root / 'script_core'
    required = [core/'physical_cuda_pool.py', core/'csrc/physical_cuda_pool.cpp']
    if not all(p.is_file() for p in required):
        raise ValueError('Runtime lacks physical pool support; prepare a NEW runtime from updated sources')
    if 'physical_cuda_pool import ensure' not in (core/'budget.py').read_text():
        raise ValueError('Runtime model worker lacks physical pool integration')
    control = root.parent/'control/model_process.py'
    if control.exists() and 'physical_cuda_pool import ensure' not in control.read_text():
        raise ValueError('Frozen resident model process lacks physical pool integration')
    # Build the exact frozen source against this environment's torch ABI.
    result = subprocess.run([sys.executable, '-c',
        'from script_core.physical_cuda_pool import build_library; print(build_library())'],
        cwd=root, env=dict(os.environ, PYTHONPATH=str(root)), check=True, capture_output=True, text=True)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True,
                        help='new experiment runtime root containing script_core')
    parser.add_argument('--check', action='store_true', help='compile and check only; no CUDA allocation')
    parser.add_argument('command', nargs=argparse.REMAINDER,
                        help='command after --, for example python new/runtime/script/runner.py supervise ...')
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    print('Physical CUDA pool runtime ready:', check_runtime(runtime), flush=True)
    print('Lease/pool MiB: source 32768/30720; training 43008/40960; evaluation 24576/22528', flush=True)
    if args.check:
        return
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        parser.error('Supply the new experiment command after --, or use --check')
    env = dict(os.environ, VIDEOAGENT_PHYSICAL_CUDA_POOL='1')
    # No inherited key: the experiment must acquire its own live shared lease.
    env.pop('VIDEOAGENT_GPU_LEASE', None)
    os.execvpe(command[0], command, env)


if __name__ == '__main__':
    main()
