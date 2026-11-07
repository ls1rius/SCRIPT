"""Explicit continuation after LMFE's zero-length string shortcut failure."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from script import controlled_memory_io as io
from script import run_mn_decoder_resume as resume

DECODER = 'MN-navigation-empty-string-enum-v4'


def prepare(prior, out):
    # Reuse the established archival/identity/cost checks, with an explicit
    # patch allowlist. No model, method prompt or canonical schema is changed.
    resume.PATCH_FILES = ('script_core/schemas.py',
                         'script/run_mn_empty_string_resume.py',
                         'script/gpu_queue.py', 'script/gpu_shared_queue.py')
    resume.DECODER = DECODER
    resume.prepare(prior, out)
    policy = io.read(out/'experiment.json')
    policy.update(runner_entry='script/run_mn_empty_string_resume.py',
        decoder_repair_scope='Equivalent decoder-only empty-string enum for maxLength=0; canonical schemas, prompts, evidence and budgets unchanged.',
        continuation_reason='user_requested_zero_length_string_decoder_repair')
    policy['resource_policy'].update(admission_mode='memory_shared',
        preference=['0','1','2','3'], maximum_utilization=100)
    io.write(out/'experiment.json', policy)
    # The previous manifest includes experiment.json; update that entry only.
    frozen = io.read(out/'frozen_hashes.json')
    frozen['experiment.json'] = io.sha(out/'experiment.json')
    io.write(out/'frozen_hashes.json', frozen)
    resume.common.verify(out)
    resume.common.report(out)


def functional(out, backend, cfg):
    resume.DECODER = DECODER
    return resume.functional(out, backend, cfg)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['prepare','queue','worker','report'])
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--prior', type=Path)
    a = p.parse_args(); out = a.out.resolve()
    if a.mode == 'prepare':
        prepare(a.prior.resolve(), out)
    elif a.mode == 'queue':
        resume.common.queue(out)
    elif a.mode == 'worker':
        resume.common.worker(out, functional_gate=functional)
    else:
        resume.common.report(out)
