"""K: freeze F evidence and independently wait for an available GPU."""
import argparse
from copy import deepcopy
import fcntl
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import controlled_memory_io as io

ARM = 'K_evidence_planner'
ARMS = ('F_sentence', 'K_all_evidence', ARM)
PARENT_ARM = 'F_lazy_script'
PLANNER_MAX_NEW_TOKENS = 8192
READER_PROTOCOL = 'K-constrained-prediction-letter-v1'
PLANNER_DECODER = 'K-nonempty-decisions-v2-compact'


def strict_answer_parser(options):
    from lmformatenforcer import RegexParser
    if not isinstance(options, list) or not 1 <= len(options) <= 26 or any(
            not isinstance(option, str) or not option.strip() for option in options):
        raise ValueError('K reader requires 1-26 nonempty options')
    return RegexParser('prediction: [' + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'[:len(options)] + ']')


def parse_strict_answer(text, options):
    strict_answer_parser(options)  # validate the option contract independently
    matched = re.fullmatch(r'prediction: ([A-Z])', text)
    option = ord(matched.group(1)) - ord('A') if matched else None
    if option is None or option >= len(options):
        return dict(option=None, method='invalid_constrained_prediction')
    return dict(option=option, method='constrained_prediction_letter')


def prepare_strict_reader(prior, out, tokenizer=None):
    """New uniform reader cohort, reusing audited plans but no legacy answers."""
    prior, out = prior.resolve(), out.resolve()
    if out.exists():
        raise ValueError('Use a fresh K strict-reader directory')
    verify(prior)
    status = io.read(prior / 'queue_status.json')
    if status['state'] != 'failed':
        raise ValueError('Reader repair requires a failed prior snapshot')
    for pid in (status.get('controller_pid'), io.read(prior / 'launch.json')['worker_pid']):
        if pid and Path('/proc', str(pid)).exists():
            raise ValueError('Prior K process is still present')
    began = time.monotonic()
    core = module('evidence_planner')
    for name in ('evidence_planner', 'g_rl', 'qa_evidence_view'):
        if io.sha(Path(module(name).__file__)) != io.sha(prior / 'control' / (name + '.py')):
            raise ValueError('Strict reader repair cannot change evidence or planner core')
    old_hashes = {str(p.relative_to(prior)): io.sha(p) for p in prior.rglob('*') if p.is_file()}
    regions = {'control', 'runtime', 'pool', 'question', 'F_sentence', 'K_all_evidence',
               'planner_input', 'all_audit', 'baseline_results'}
    roots = {'cohort.json', 'evaluation_only.json', 'qa_config.json', 'qa_expected_artifacts.json'}
    for name in io.read(prior / 'frozen_hashes.json'):
        if name in roots or Path(name).parts[0] in regions or name == 'validation/cpu_input_preflight.json':
            target = out / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(prior / name, target)
    for name in ('run_evidence_planner_experiment.py', 'gpu_queue.py'):
        shutil.copy2(Path(__file__).parent / name, out / 'control' / name)
    history = out / 'resume_history' / prior.name
    history.mkdir(parents=True)
    shutil.copytree(prior / 'runs', history / 'legacy_runs')
    shutil.copytree(prior / 'functional', history / 'legacy_functional')
    for name in ('queue_status.json', 'launch.json', 'setup.json', 'report.json', 'experiment.json',
                 'progress.json', 'frozen_hashes.json', 'worker.log', 'controller.log'):
        if (prior / name).exists():
            shutil.copy2(prior / name, history / name)
    io.write(history / 'source_files.json', old_hashes)
    ids = io.read(out / 'cohort.json')
    saved = {}
    plan_files = ('started.json', 'planner_output.json', 'planner_output_input.json',
                  'plan.json', 'plan_audit.json', 'K_input.json')
    for uid in ids:
        question = io.read(out / 'question' / (uid + '.json'))
        if set(question) != {'question_id', 'video_id', 'question', 'options'} or question['question_id'] != uid:
            raise ValueError('K question contract/identity changed: ' + uid)
        strict_answer_parser(question['options'])
        pool = io.read(out / 'pool' / (uid + '.json'))
        if core.planner_input(question, pool, uid) != io.read(out / 'planner_input' / (uid + '.json')):
            raise ValueError('K planner input does not match its question/evidence: ' + uid)
        all_text, all_audit = core.render(pool, core.keep_all(pool))
        if all_audit != io.read(out / 'all_audit' / (uid + '.json')):
            raise ValueError('K full-evidence source audit changed: ' + uid)
        if core.final_input(io.read(out / 'F_sentence' / (uid + '.json')), all_text, io.parts) != io.read(out / 'K_all_evidence' / (uid + '.json')):
            raise ValueError('K full-evidence control changed fixed input regions: ' + uid)
        folder = prior / 'runs' / uid
        if not folder.exists():
            continue
        pool = io.read(out / 'pool' / (uid + '.json'))
        usage = io.read(folder / 'planner_output.json')
        if not usage['ended_normally'] or io.read(folder / 'planner_output_input.json') != io.read(out / 'planner_input' / (uid + '.json')):
            raise ValueError('Saved planner generation/input mismatch: ' + uid)
        plan = core.decode_plan(usage['raw'], pool)
        description, audit = core.render(pool, plan)
        selected = core.final_input(io.read(out / 'K_all_evidence' / (uid + '.json')), description, io.parts)
        if (plan != io.read(folder / 'plan.json') or audit != io.read(folder / 'plan_audit.json')
                or selected != io.read(folder / 'K_input.json')):
            raise ValueError('Saved plan/source audit/input mismatch: ' + uid)
        saved[uid] = {}
        for name in plan_files:
            target = out / 'runs' / uid / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / name, target)
            saved[uid][name] = io.sha(target)
    policy = io.read(prior / 'experiment.json')
    if len(ids) != len(set(ids)) or len(ids) != policy['planned']:
        raise ValueError('K cohort is incomplete or has duplicate identities')
    old_answers = [io.read(p) for p in (prior / 'runs').glob('*/*/answer_output.json')]
    overhead = io.read(prior / 'setup.json')['seconds']
    if not policy.get('resume_from') or policy.get('rerun_functional_gates'):
        overhead += sum(io.read(p)['seconds'] for p in (prior / 'functional').glob('*_output.json'))
    policy.update(created_at=io.now(), reader_protocol=READER_PROTOCOL,
        final_answer_parser=READER_PROTOCOL, reuse_plans_only=True, plan_decoder=PLANNER_DECODER,
        resume_from=str(prior), resume_manifest_sha256=io.sha(prior / 'frozen_hashes.json'),
        resume_saved_cases=saved, resume_repaired_answers=[], resume_replanned_cases=[],
        rerun_functional_gates=True, resume_preparation_seconds=time.monotonic()-began,
        prior_resume_preparation_seconds=policy.get('prior_resume_preparation_seconds', 0.) + policy.get('resume_preparation_seconds', 0.),
        prior_runtime_seconds=policy.get('prior_runtime_seconds', 0.) + overhead + sum(a['seconds'] for a in old_answers),
        prior_reader_calls=policy.get('prior_reader_calls', 0) + len(old_answers),
        prior_reader_seconds=policy.get('prior_reader_seconds', 0.) + sum(a['seconds'] for a in old_answers),
        prior_reader_input_tokens=policy.get('prior_reader_input_tokens', 0) + sum(a['input_tokens'] for a in old_answers),
        prior_reader_output_tokens=policy.get('prior_reader_output_tokens', 0) + sum(a['output_tokens'] for a in old_answers),
        protocol_policy='All three arms regenerate every final answer with one constrained letter protocol. Legacy answers are archived, never pooled.',
        failure_policy='No automatic retries or semantic answer guessing; stop on invalid generation, gate, source audit, or runtime failure.')
    policy['resource_policy'] = dict(policy['resource_policy'], selection='most_free_memory')
    io.write(out / 'experiment.json', policy)
    if tokenizer is None:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(policy['model'], local_files_only=True)
    counts = {}
    cap = io.read(out / 'qa_config.json')['models']['max_input_tokens']
    for uid in ids:
        counts[uid] = {region: token_count(tokenizer, io.read(out / region / (uid + '.json')))
            for region in ('F_sentence', 'K_all_evidence', 'planner_input')}
        if max(counts[uid].values()) > cap:
            raise ValueError('Strict reader preflight overflow: ' + uid)
    io.write(out / 'validation/strict_reader_cpu_preflight.json', dict(passed=True,
        model_loaded=False, benchmark_labels_used=False, questions=len(ids),
        reused_plans=len(saved), reused_answers=0, input_tokens=counts,
        reader_protocol=READER_PROTOCOL, max_new_tokens=io.read(out / 'qa_config.json')['models']['generation']['max_new_tokens']))
    policy['resume_preparation_seconds'] = time.monotonic()-began
    io.write(out / 'experiment.json', policy)
    if old_hashes != {str(p.relative_to(prior)): io.sha(p) for p in prior.rglob('*') if p.is_file()}:
        raise ValueError('Prior snapshot changed during repair preparation')
    io.write(out / 'frozen_hashes.json', {str(p.relative_to(out)): io.sha(p)
        for p in out.rglob('*') if p.is_file()})
    io.write(out / 'queue_status.json', dict(state='prepared', updated_at=io.now()))
    report(out)


def nonempty_plan_parser(schema):
    """Constrain the final value when all preceding decisions were skip.

    This enforces the existing nonempty audit rule during decoding. It does not
    change earlier decisions or choose a record after generation has finished.
    """
    from lmformatenforcer import CharacterLevelParser, JsonSchemaParser

    class NonemptyPlanParser(CharacterLevelParser):
        def __init__(self, wrapped, in_string=False, escaped=False, value='',
                     key=True, completed=0, retained=False):
            super().__init__(wrapped.config)
            self.wrapped = wrapped
            self.in_string, self.escaped, self.value = in_string, escaped, value
            self.key, self.completed, self.retained = key, completed, retained

        def get_allowed_characters(self):
            allowed = self.wrapped.get_allowed_characters()
            if (self.in_string and not self.key and not self.value
                    and self.completed == len(schema['required']) - 1 and not self.retained):
                # Reject the skip branch before it can become a dead end. Values
                # are fixed ASCII enums, so escaped spellings are also excluded.
                allowed = allowed.replace('s', '').replace('\\', '')
            return allowed

        def add_character(self, char):
            if char not in self.get_allowed_characters():
                raise ValueError('K decoder rejects an all-skip plan')
            inside, escaped, value = self.in_string, self.escaped, self.value
            key, completed, retained = self.key, self.completed, self.retained
            if inside:
                if escaped:
                    value += char
                    escaped = False
                elif char == '\\':
                    value += char
                    escaped = True
                elif char == '"':
                    inside = False
                    if not key:
                        completed += 1
                        retained = retained or json.loads('"' + value + '"') != 'skip'
                    key = not key
                    value = ''
                else:
                    value += char
            elif char == '"':
                inside = True
            return NonemptyPlanParser(self.wrapped.add_character(char), inside, escaped,
                value, key, completed, retained)

        def can_end(self):
            return self.retained and self.wrapped.can_end()

    parser = JsonSchemaParser(schema)
    parser.config.force_json_field_order = True
    parser.config.max_consecutive_whitespaces = 1
    return NonemptyPlanParser(parser)


def parse_final_answer(text, options):
    """K-only, gold-blind extension for a unique verbatim first option sentence."""
    parser = module('g_rl')
    parsed = parser.parse_answer(text, options)
    if parsed['option'] is not None:
        return parsed
    clean = text.replace('**', '').replace('`', '').strip()
    declared = re.findall(r'(?im)^\s*prediction\s*:\s*([^\n]*)$', clean)
    if len(declared) != 1:
        return parsed
    value = declared[0].strip()
    # Require a complete sentence of at least five words, never a bare person C.
    if not re.fullmatch(r'[^.!?]+[.!?]', value) or len(value.split()) < 5:
        return parsed
    matches = []
    for index, option in enumerate(options):
        first = re.split(r'(?<=[.!?])\s+', option.strip(), maxsplit=1)[0]
        if parser.canonical(first) == parser.canonical(value):
            matches.append(index)
    if len(matches) == 1:
        return dict(option=matches[0], method='unique_verbatim_first_sentence')
    return parsed


def prepare_resume(prior, out, repair_empty_plan=False):
    """Explicit repair continuation; the failed source remains byte-for-byte intact."""
    prior, out = prior.resolve(), out.resolve()
    if out.exists():
        raise ValueError('Use a fresh K continuation directory')
    verify(prior)
    state = io.read(prior / 'queue_status.json')
    if state['state'] != 'failed':
        raise ValueError('Continuation requires a failed K snapshot')
    pids = [state.get('controller_pid'), io.read(prior / 'launch.json')['worker_pid']]
    if any(pid and Path('/proc', str(pid)).exists() for pid in pids):
        raise ValueError('Prior K process is still present')
    for gate in ('functional_small.json', 'functional.json'):
        if not io.read(prior / 'validation' / gate)['passed']:
            raise ValueError('Continuation requires both passed synthetic gates')
    began = time.monotonic()
    manifest = io.read(prior / 'frozen_hashes.json')
    for name in manifest:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prior / name, target)
    for region in ('runs', 'functional'):
        shutil.copytree(prior / region, out / region, dirs_exist_ok=True)
    for name in ('functional_small.json', 'functional.json'):
        shutil.copy2(prior / 'validation' / name, out / 'validation' / name)
    history = out / 'resume_history' / prior.name
    history.mkdir(parents=True)
    for name in ('queue_status.json', 'launch.json', 'setup.json', 'report.json', 'progress.json',
                 'controller.log', 'worker.log', 'frozen_hashes.json'):
        if (prior / name).exists():
            shutil.copy2(prior / name, history / name)
    shutil.copy2(Path(__file__), out / 'control' / Path(__file__).name)
    core = module('evidence_planner')
    for name in ('evidence_planner', 'g_rl'):
        if io.sha(Path(module(name).__file__)) != io.sha(prior / 'control' / (name + '.py')):
            raise ValueError('Continuation cannot change the saved planner/shared parser')
    repaired, saved, replanned = [], {}, []
    ids = io.read(out / 'cohort.json')
    for uid in ids:
        folder = out / 'runs' / uid
        if not folder.exists():
            continue
        if (folder / 'failure.json').exists():
            target = history / 'case_failures' / (uid + '.json')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(folder / 'failure.json', target)
        pool = io.read(out / 'pool' / (uid + '.json'))
        usage = io.read(folder / 'planner_output.json')
        if not usage['ended_normally'] or io.read(folder / 'planner_output_input.json') != io.read(out / 'planner_input' / (uid + '.json')):
            raise ValueError('Inherited planner call/input invalid: ' + uid)
        try:
            plan = core.decode_plan(usage['raw'], pool)
        except ValueError as exc:
            decisions = json.loads(usage['raw'])
            expected_ids = {r['id'] for r in pool['records']}
            if (not repair_empty_plan or str(exc) != 'K cannot discard every evidence record'
                    or set(decisions) != expected_ids or set(decisions.values()) != {'skip'}
                    or any(folder.glob('*/answer_output.json'))):
                raise
            replanned.append(dict(question_id=uid, seconds=usage['seconds'],
                input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'],
                output_sha256=io.sha(folder / 'planner_output.json'), reason=str(exc)))
            shutil.move(folder, history / ('rejected_plan_' + uid))
            continue
        description, audit = core.render(pool, plan)
        selected = core.final_input(io.read(out / 'K_all_evidence' / (uid + '.json')), description, io.parts)
        if (plan != io.read(folder / 'plan.json') or audit != io.read(folder / 'plan_audit.json')
                or selected != io.read(folder / 'K_input.json')):
            raise ValueError('Inherited plan audit/input invalid: ' + uid)
        expected = dict(F_sentence=io.read(out / 'F_sentence' / (uid + '.json')),
            K_all_evidence=io.read(out / 'K_all_evidence' / (uid + '.json')), K_evidence_planner=selected)
        for arm in ARMS:
            path = folder / arm / 'result.json'
            if not path.exists():
                continue
            result = io.read(path)
            answer = io.read(path.with_name('answer_output.json'))
            inputs = io.read(path.with_name('answer_output_input.json'))
            if (inputs != expected[arm] or not answer['ended_normally']
                    or result['input_sha256'] != core.digest(inputs)
                    or any(result[k] != answer[v] for k, v in (
                        ('raw_response', 'raw'), ('seconds', 'seconds'),
                        ('input_tokens', 'input_tokens'), ('output_tokens', 'output_tokens')))):
                raise ValueError('Inherited answer binding invalid: ' + uid + '/' + arm)
            parse = parse_strict_answer if io.read(prior / 'experiment.json').get('reader_protocol') == READER_PROTOCOL else parse_final_answer
            parsed = parse(answer['raw'], io.read(out / 'question' / (uid + '.json'))['options'])
            if parsed['option'] is None:
                raise ValueError('Inherited answer is still ambiguous: ' + uid + '/' + arm)
            if result.get('error'):
                repaired.append(dict(question_id=uid, arm=arm, old_result_sha256=io.sha(path),
                    method=parsed['method'], model_regenerated=False))
                result.update(selected_option=str(parsed['option']), error=None, parser=parsed['method'])
                io.write(path, result)
            elif result['selected_option'] != str(parsed['option']):
                raise ValueError('Repair changed a previously valid answer')
        saved[uid] = {str(p.relative_to(folder)): io.sha(p) for p in folder.rglob('*') if p.is_file()}
    policy = io.read(out / 'experiment.json')
    # Saved calls stay in runs and report costs exactly once. Only old loading/gates
    # move to prior_runtime_seconds; new model loading is recorded separately.
    overhead = io.read(prior / 'setup.json')['seconds']
    if not policy.get('resume_from') or policy.get('rerun_functional_gates'):
        overhead += sum(io.read(p)['seconds'] for p in (prior / 'functional').glob('*_output.json'))
    if repair_empty_plan:
        shutil.move(out / 'functional', history / 'inherited_functional')
        for name in ('functional_small.json', 'functional.json'):
            shutil.move(out / 'validation' / name, history / ('inherited_' + name))
    policy.update(resume_from=str(prior), resume_manifest_sha256=io.sha(prior / 'frozen_hashes.json'),
        resume_saved_cases=saved, resume_repaired_answers=repaired,
        resume_preparation_seconds=time.monotonic()-began,
        prior_runtime_seconds=policy.get('prior_runtime_seconds', 0.) + overhead + sum(p['seconds'] for p in replanned),
        prior_resume_preparation_seconds=policy.get('prior_resume_preparation_seconds', 0.) + policy.get('resume_preparation_seconds', 0.),
        prior_failed_planner_calls=policy.get('prior_failed_planner_calls', 0) + len(replanned),
        prior_failed_planner_seconds=policy.get('prior_failed_planner_seconds', 0.) + sum(p['seconds'] for p in replanned),
        prior_failed_planner_input_tokens=policy.get('prior_failed_planner_input_tokens', 0) + sum(p['input_tokens'] for p in replanned),
        prior_failed_planner_output_tokens=policy.get('prior_failed_planner_output_tokens', 0) + sum(p['output_tokens'] for p in replanned),
        resume_replanned_cases=replanned, rerun_functional_gates=repair_empty_plan,
        plan_decoder='K-nonempty-decisions-v1',
        final_answer_parser=policy.get('reader_protocol', 'K-unique-verbatim-first-sentence-v1'),
        resume_policy='Explicit user-authorized continuation; reuse validated calls, no automatic retries.')
    io.write(out / 'experiment.json', policy)
    io.write(out / 'frozen_hashes.json', {str(p.relative_to(out)): io.sha(p)
        for p in out.rglob('*') if p.is_file() and p.name != 'frozen_hashes.json'})
    io.write(out / 'queue_status.json', dict(state='prepared', updated_at=io.now()))
    report(out)


def verify_saved_cases(out, policy):
    for uid, files in policy.get('resume_saved_cases', {}).items():
        for name, expected in files.items():
            if io.sha(out / 'runs' / uid / name) != expected:
                raise ValueError('Inherited K call changed: ' + uid + '/' + name)


def module(name):
    if (Path(__file__).parent / (name + '.py')).exists():
        return importlib.import_module(name)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    return importlib.import_module('script_core.' + name)


def verify(root):
    for name, expected in io.read(root / 'frozen_hashes.json').items():
        if io.sha(root / name) != expected:
            raise ValueError('Frozen artifact changed: ' + str(root / name))


def token_count(tokenizer, inputs):
    return len(tokenizer.apply_chat_template([
        dict(role='system', content=inputs['head']),
        dict(role='user', content=inputs['prompts'][0])], tokenize=True, add_generation_prompt=True))


def dependency_ready(policy):
    # F evidence is already sealed. A scheduling predecessor is optional,
    # and independent K must neither wait for nor inherit a failure from J.
    if not policy.get('after_queue'):
        return True
    root = Path(policy['after_queue'])
    if io.sha(root / 'frozen_hashes.json') != policy['after_manifest_sha256']:
        raise ValueError('K predecessor manifest changed')
    status = io.read(root / 'queue_status.json')['state']
    if status in {'failed', 'stopped', 'superseded', 'cancelled'} or (root / 'failures/queue.json').exists():
        raise RuntimeError('K scheduling predecessor failed: ' + str(root))
    if status != 'complete':
        return False
    output = Path(policy['after_output'])
    if io.read(output / 'queue_status.json')['state'] != 'complete' or not io.read(output / 'report.json')['complete']:
        raise RuntimeError('J queue completed without a complete J output')
    return True


def prepare(parent, out, after=None, tokenizer=None, prior=None):
    """CPU-only preparation, including exact-token checks; no model loading."""
    if out.exists():
        raise ValueError('Use a fresh K output directory')
    parent, out = parent.resolve(), out.resolve()
    after = after.resolve() if after is not None else None
    verify(parent)
    if after is not None:
        verify(after)
    if io.read(parent / 'experiment.json')['arm'] != PARENT_ARM:
        raise ValueError('K requires the completed frozen F experiment')
    if not io.read(parent / 'report.json')['complete'] or not io.read(parent / 'validation/functional.json')['passed']:
        raise ValueError('F must have complete answers and a passed functional gate')
    after_plan = io.read(after / 'plan.json') if after is not None else {}
    if after is not None and 'output_J' not in after_plan:
        raise ValueError('K must be scheduled after the current J queue')
    prior_seconds, prior_runtime_seconds, prior_record = 0., 0., None
    if prior is not None:
        prior = prior.resolve()
        verify(prior)
        state = io.read(prior / 'queue_status.json')['state']
        if state not in {'superseded', 'failed'} or (state == 'superseded' and (prior / 'launch.json').exists()):
            raise ValueError('Only a superseded CPU-only or failed K can be a prior revision')
        if (prior / 'launch.json').exists():
            pid = io.read(prior / 'launch.json')['worker_pid']
            if Path('/proc', str(pid)).exists():
                raise ValueError('Prior K worker is still present; refusing a concurrent replacement')
        old = io.read(prior / 'experiment.json')
        if old['arm'] != ARM or Path(old['parent_F']).resolve() != parent:
            raise ValueError('Prior K must use the same F parent')
        prior_seconds = old.get('prior_preparation_seconds', 0.) + old['cpu_preparation_seconds']
        costs = [p for region in ('functional', 'runs') for p in (prior / region).rglob('*_output.json')]
        if (prior / 'setup.json').exists():
            costs.append(prior / 'setup.json')
        prior_runtime_seconds = old.get('prior_runtime_seconds', 0.) + sum(io.read(p)['seconds'] for p in costs)
        prior_record = dict(path=str(prior), state=state, manifest_sha256=io.sha(prior/'frozen_hashes.json'),
            measured_cost_files={str(p.relative_to(prior)):dict(sha256=io.sha(p),seconds=io.read(p)['seconds']) for p in costs})
    core, renderer = module('evidence_planner'), module('qa_evidence_view')
    cfg = io.read(parent / 'qa_config.json')
    if cfg['models']['generation']['do_sample'] or cfg['models']['precision'] != 'bfloat16':
        raise ValueError('K expects the original bf16 greedy F QA protocol')
    ids = io.read(parent / 'cohort.json')
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('Invalid F cohort')
    prepared, bindings = {}, {}
    started = time.monotonic()
    for uid in ids:
        folder = parent / 'materialized' / uid
        for name, expected in io.read(folder / 'sealed.json').items():
            if io.sha(folder / name) != expected:
                raise ValueError('F materialization seal changed: ' + uid)
        path = parent / 'final_inputs' / PARENT_ARM / (uid + '.json')
        answer = io.read(parent / 'runs' / PARENT_ARM / uid / 'result.json')
        if answer.get('error') or answer.get('input_sha256') != io.sha(path):
            raise ValueError('F answer/input binding failed: ' + uid)
        original, memory = io.read(path), io.read(folder / 'memory.json')
        # Verify against the legacy renderer, not the newly introduced sentence view.
        if renderer.compact_description(memory)[0] != io.parts(original)[1]:
            raise ValueError('Archived F evidence differs from the actual F input: ' + uid)
        question = io.read(parent / 'packets' / (uid + '.json'))['packet']['question']
        question = {k: question[k] for k in ('question_id', 'video_id', 'question', 'options')}
        pool = core.build_pool(memory, original, io.parts, renderer._fact_sentence)
        prefix, _, suffix = io.parts(original)
        f_sentence = deepcopy(original)
        f_sentence['prompts'][0] = prefix + renderer.narrative_description(memory)[0] + suffix
        if io.fixed(f_sentence) != io.fixed(original):
            raise ValueError('F sentence control changed fixed QA regions')
        all_text, audit = core.render(pool, core.keep_all(pool))
        all_input = core.final_input(original, all_text, io.parts)
        planner = core.planner_input(question, pool, uid)
        prepared[uid] = dict(question=question, pool=pool, F_sentence=f_sentence,
            K_all_evidence=all_input, planner_input=planner, all_audit=audit)
        bindings[uid] = dict(parent_input_sha256=io.sha(path),
            parent_memory_sha256=io.sha(folder / 'memory.json'),
            parent_seal_sha256=io.sha(folder / 'sealed.json'))
    if tokenizer is None:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(cfg['models']['vlm'], local_files_only=True)
    preflight = {}
    for uid, item in prepared.items():
        counts = {name: token_count(tokenizer, item[name])
            for name in ('F_sentence', 'K_all_evidence', 'planner_input')}
        if max(counts.values()) > cfg['models']['max_input_tokens']:
            raise ValueError('K full-context input overflow; no truncation: ' + uid + ' ' + str(counts))
        # Bound the compact decision map using the longest tokenized group label.
        refs = [r['id'] for r in item['pool']['records']]
        values = ['keep','skip','uncertain'] + ['group:'+r for r in refs]
        longest_value = max(len(tokenizer.encode(json.dumps(v))) for v in values)
        output_bound = sum(len(tokenizer.encode(json.dumps(r))) + longest_value + 4 for r in refs) + 128
        if output_bound > PLANNER_MAX_NEW_TOKENS:
            raise ValueError('K plan output budget insufficient: ' + uid)
        preflight[uid] = dict(**counts, planner_output_token_bound=output_bound)
    out.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    for name in ('cohort.json', 'evaluation_only.json', 'qa_config.json', 'qa_expected_artifacts.json'):
        shutil.copy2(parent / name, out / name)
    control = out / 'control'
    control.mkdir()
    for name in ('run_evidence_planner_experiment.py', 'controlled_memory_io.py', 'gpu_queue.py'):
        shutil.copy2(Path(__file__).parent / name, control / name)
    for name in ('evidence_planner', 'qa_evidence_view', 'g_rl'):
        shutil.copy2(Path(module(name).__file__), control / (name + '.py'))
    runtime = out / 'runtime'
    runtime.mkdir()
    for name in ('model.py', 'prompts.py'):
        shutil.copy2(parent / 'runtime/qa' / name, runtime / name)
    for uid, item in prepared.items():
        for name, value in item.items():
            io.write(out / name / (uid + '.json'), value)
        io.write(out / 'baseline_results' / (uid + '.json'),
            io.read(parent / 'runs' / PARENT_ARM / uid / 'result.json'))
    parent_report = io.read(parent / 'report.json')
    stages = parent_report.get('measured_seconds_by_stage', {})
    io.write(out / 'experiment.json', dict(created_at=io.now(), arm=ARM, version=core.VERSION,
        parent_F=str(parent), parent_memory_variant='F', planned=len(ids), arms=list(ARMS),
        primary_comparison='K_evidence_planner_vs_K_all_evidence',
        secondary_comparison='K_evidence_planner_vs_F_sentence',
        after_queue=str(after) if after is not None else None, after_output=after_plan.get('output_J'),
        after_manifest_sha256=io.sha(after / 'frozen_hashes.json') if after is not None else None,
        parent_manifest_sha256=io.sha(parent / 'frozen_hashes.json'), bindings=bindings,
        inherited_F_construction_seconds=sum(stages.get(k, 0) for k in ('setup', 'functional', 'materialized')),
        inherited_F_prior_development_seconds=parent_report.get('prior_development_seconds', 0),
        cpu_preparation_seconds=time.monotonic()-started, planner_max_new_tokens=PLANNER_MAX_NEW_TOKENS,
        prior_preparation=str(prior) if prior is not None else None, prior_preparation_seconds=prior_seconds,
        prior_runtime_seconds=prior_runtime_seconds, prior_revision=prior_record,
        model=cfg['models']['vlm'], training=False, adapter=None, new_retrieval=False, new_extraction_calls=0,
        reader_protocol=READER_PROTOCOL, final_answer_parser=READER_PROTOCOL, plan_decoder=PLANNER_DECODER,
        planner_and_reader='Independent prompts and call records; one frozen original model instance reused serially.',
        candidate_policy='Original clauses and every F fact, temporal and visual record are archived; exact text/status duplicates share all sources.',
        merge_policy='Group without paraphrase; nonidentical statements stay separate within the paragraph; no inferred duration, identity or counts.',
        failure_policy='No automatic retries, hidden truncation or answer fallback. Stop this experiment on dependency, gate, plan or runtime failure.',
        limitations='Text planning does not prove evidence sufficiency, semantic equivalence, or correctness. All-source narrative control isolates rendering effects.',
        resource_policy=dict(preference=['0', '1', '2', '3'], minimum_free_mib=32768,
            maximum_utilization=100, stable_observations=3, selection='most_free_memory')))
    io.write(out / 'validation/cpu_input_preflight.json', dict(passed=True, model_loaded=False,
        benchmark_labels_used=False, questions=len(ids), input_tokens=preflight,
        maximum_input_tokens=max(max(c[k] for k in ('F_sentence','K_all_evidence','planner_input')) for c in preflight.values()),
        total_candidate_records=sum(len(i['pool']['records']) for i in prepared.values())))
    io.write(out / 'frozen_hashes.json', {str(p.relative_to(out)): io.sha(p)
        for p in out.rglob('*') if p.is_file()})
    io.write(out / 'queue_status.json', dict(state='prepared', updated_at=io.now()))
    report(out)


def report(out):
    policy = io.read(out / 'experiment.json')
    arms = tuple(policy.get('arms', ARMS))
    arm = policy.get('arm', ARM)
    label = policy.get('experiment_label', 'K')
    if len(arms) != 3 or len(set(arms)) != 3 or arm != arms[2]:
        raise ValueError('Invalid evidence-planner comparison arms')
    ids, truth = io.read(out / 'cohort.json'), io.read(out / 'evaluation_only.json')
    results = {a: {} for a in arms}
    for a in arms:
        for uid in ids:
            path = out / 'runs' / uid / a / 'result.json'
            if path.exists():
                results[a][uid] = io.read(path)
                if policy.get('reader_protocol') and results[a][uid].get('reader_protocol') != policy['reader_protocol']:
                    raise ValueError('Mixed K reader protocols in active results: ' + uid + '/' + a)

    def good(a, uid):
        r = results[a][uid]
        return not r.get('error') and str(r.get('selected_option')) == str(truth[uid])

    summaries = {}
    for arm, rows in results.items():
        n, correct = len(rows), sum(good(arm, uid) for uid in rows)
        summaries[arm] = dict(completed=n, planned=len(ids), correct=correct,
            successful=sum(not r.get('error') for r in rows.values()), pending=len(ids)-n,
            errors=sum(bool(r.get('error')) for r in rows.values()),
            accuracy_among_completed=correct/n if n else None,
            accuracy=correct/n if n == len(ids) else None,
            mean_input_tokens=statistics.mean(r['input_tokens'] for r in rows.values()) if n else None,
            mean_qa_seconds=statistics.mean(r['seconds'] for r in rows.values()) if n else None)
    comparisons = {}
    for baseline in arms[:2]:
        paired = [u for u in ids if u in results[arm] and u in results[baseline]]
        comparisons[arm + '_vs_' + baseline] = dict(paired=len(paired),
            baseline_correct=sum(good(baseline, u) for u in paired),
            **{label+'_correct':sum(good(arm, u) for u in paired)},
            corrected=[u for u in paired if good(arm, u) and not good(baseline, u)],
            regressed=[u for u in paired if not good(arm, u) and good(baseline, u)])
    plans = [io.read(out / 'runs' / u / 'plan_audit.json') for u in ids
        if (out / 'runs' / u / 'plan_audit.json').exists()]
    calls = []
    for u in ids:
        path = out / 'runs' / u / 'planner_output.json'
        if path.exists():
            calls.append(io.read(path))
    fully_completed = sum(all(u in results[a] and not results[a][u].get('error') for a in arms) for u in ids)
    last_only = [u for u in ids if (out / 'runs' / u / 'plan.json').exists()
        and io.read(out / 'runs' / u / 'plan.json')['skip'] ==
            [r['id'] for r in io.read(out / 'pool' / (u + '.json'))['records']][:-1]]
    summary = dict(updated_at=io.now(), planned=len(ids), completed=fully_completed,
        reader_protocol=policy.get('reader_protocol', 'legacy_free_text'),
        reused_plans=len(policy.get('resume_saved_cases', {})) if policy.get('reuse_plans_only') else 0,
        legacy_answers_pooled=False, last_record_only_plans=last_only,
        complete=all(summaries[a]['completed'] == len(ids) and not summaries[a]['errors'] for a in arms),
        arms=summaries, comparisons=comparisons, primary_comparison=policy['primary_comparison'],
        planner_calls=len(calls), planner_input_tokens=sum(c['input_tokens'] for c in calls),
        planner_output_tokens=sum(c['output_tokens'] for c in calls), planner_seconds=sum(c['seconds'] for c in calls),
        mean_planner_seconds=statistics.mean(c['seconds'] for c in calls) if calls else None,
        **{'mean_'+label+'_planner_plus_final_seconds':statistics.mean(results[arm][u]['seconds'] +
            io.read(out / 'runs' / u / 'planner_output.json')['seconds'] for u in results[arm]) if results[arm] else None},
        plans_completed=len(plans), retained_records=sum(p['retained_records'] for p in plans),
        skipped_records=sum(p['skipped_records'] for p in plans), merge_groups=sum(p['merge_groups'] for p in plans),
        **{'inherited_'+policy.get('parent_memory_variant','F')+'_construction_seconds':policy.get(
            'inherited_construction_seconds', policy.get('inherited_F_construction_seconds', 0.)),
            'inherited_'+policy.get('parent_memory_variant','F')+'_prior_development_seconds':policy.get(
                'inherited_prior_development_seconds',policy.get('inherited_F_prior_development_seconds', 0.))},
        cpu_preparation_seconds=policy['cpu_preparation_seconds'],
        prior_preparation_seconds=policy.get('prior_preparation_seconds', 0.), new_extraction_calls=0, training=False,
        prior_runtime_seconds=policy.get('prior_runtime_seconds', 0.),
        prior_reader_calls=policy.get('prior_reader_calls', 0),
        prior_reader_seconds=policy.get('prior_reader_seconds', 0.),
        prior_reader_input_tokens=policy.get('prior_reader_input_tokens', 0),
        prior_reader_output_tokens=policy.get('prior_reader_output_tokens', 0),
        resume_preparation_seconds=policy.get('resume_preparation_seconds', 0.),
        prior_resume_preparation_seconds=policy.get('prior_resume_preparation_seconds', 0.),
        prior_failed_planner_calls=policy.get('prior_failed_planner_calls', 0),
        prior_failed_planner_seconds=policy.get('prior_failed_planner_seconds', 0.),
        prior_failed_planner_input_tokens=policy.get('prior_failed_planner_input_tokens', 0),
        prior_failed_planner_output_tokens=policy.get('prior_failed_planner_output_tokens', 0),
        resume_from=policy.get('resume_from'),
        model_setup_seconds=io.read(out / 'setup.json')['seconds'] if (out / 'setup.json').exists() else 0.,
        new_functional_seconds=sum(io.read(p)['seconds'] for p in (out / 'functional').glob('*_output.json'))
            if not policy.get('resume_from') or policy.get('rerun_functional_gates') else 0.,
        token_scope='Each arm reports final-answer input tokens; K planning input/output tokens and time are separate.',
        timing_policy='Shared GPU observations, not a controlled speed ratio. Model loading and synthetic gate are separate setup costs.')
    io.write(out / 'report.json', summary)
    return summary


def verify_model(out):
    expected = io.read(out / 'qa_expected_artifacts.json')['vlm']
    for name, sha in expected['files'].items():
        if name.startswith('.cache/'):
            continue
        if io.sha(Path(expected['path']) / name) != sha:
            raise ValueError('Original Qwen model/tokenizer fingerprint changed: ' + name)


def worker(out):
    verify(out)
    if (out / 'setup.json').exists():
        raise ValueError('K worker already attempted; no automatic retries')
    verify_model(out)
    policy = io.read(out / 'experiment.json')
    core = module(policy.get('planner_core', 'evidence_planner'))
    arms = tuple(policy.get('arms', ARMS))
    planned_arm = policy.get('arm', ARM)
    label = policy.get('experiment_label', 'K')
    selected_filename = policy.get('selected_input_filename', 'K_input.json')
    if len(arms) != 3 or len(set(arms)) != 3 or planned_arm != arms[2]:
        raise ValueError('Invalid evidence-planner comparison arms')
    if policy.get('reader_protocol') not in (None, READER_PROTOCOL):
        raise ValueError('Unsupported K reader protocol')
    strict_reader = policy.get('reader_protocol') == READER_PROTOCOL
    verify_saved_cases(out, policy)
    sys.path.insert(0, str(out / 'runtime'))
    from model import Qwen
    import torch
    from lmformatenforcer.integrations.transformers import (
        build_transformers_prefix_allowed_tokens_fn, build_token_enforcer_tokenizer_data)
    cfg = io.read(out / 'qa_config.json')
    began = time.monotonic()
    model = Qwen(cfg['models']['vlm'], temperature=0, device='cuda',
        max_new_tokens=cfg['models']['generation']['max_new_tokens'])
    tokenizer_data = build_token_enforcer_tokenizer_data(model.tokenizer)
    io.write(out / 'setup.json', dict(seconds=time.monotonic()-began, model=cfg['models']['vlm'],
        training=False, adapter=None, precision='bfloat16', attention='sdpa'))

    def generate(inputs, path, schema=None, answer_options=None):
        if policy.get('resume_from') and path.exists():
            usage = io.read(path)
            if not usage['ended_normally'] or io.read(path.with_name(path.stem + '_input.json')) != inputs:
                raise ValueError('Inherited generation/input mismatch; no retry')
            return usage
        io.write(path.with_name(path.stem + '_input.json'), inputs)
        messages = [dict(role='system', content=inputs['head']), dict(role='user', content=inputs['prompts'][0])]
        encoded = model.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
            return_dict=True, return_tensors='pt').to(model.model.device)
        n = int(encoded.input_ids.shape[1])
        if n > cfg['models']['max_input_tokens']:
            raise ValueError('K input overflow; no truncation')
        kwargs = dict(do_sample=False, max_new_tokens=PLANNER_MAX_NEW_TOKENS if schema else model.max_new_tokens)
        if schema:
            json_parser = nonempty_plan_parser(schema)
            kwargs['prefix_allowed_tokens_fn'] = build_transformers_prefix_allowed_tokens_fn(tokenizer_data, json_parser)
        elif strict_reader:
            kwargs['prefix_allowed_tokens_fn'] = build_transformers_prefix_allowed_tokens_fn(
                tokenizer_data, strict_answer_parser(answer_options))
        began = time.monotonic()
        with torch.inference_mode():
            output = model.model.generate(**encoded, **kwargs)
        tokens = output[0, n:]
        usage = dict(raw=model.tokenizer.decode(tokens, skip_special_tokens=True).strip(), input_tokens=n,
            output_tokens=len(tokens), seconds=time.monotonic()-began)
        if strict_reader:
            usage['decoder'] = PLANNER_DECODER if schema else READER_PROTOCOL
        eos = getattr(getattr(model.model, 'generation_config', None), 'eos_token_id', model.tokenizer.eos_token_id)
        eos = [eos] if isinstance(eos, int) else eos or []
        usage['ended_normally'] = bool(len(tokens)) and int(tokens[-1]) in eos
        io.write(path, usage)
        if not usage['ended_normally']:
            raise ValueError('K generation did not terminate normally; no retry')
        return usage

    if not policy.get('resume_from') or policy.get('rerun_functional_gates'):
        # Functional gate uses synthetic evidence only; no benchmark labels or scores.
        probe = dict(version=core.VERSION, records=[
            dict(id='R0', text='C wipes the table using a cloth.', qualification='source assertion',
                kinds=['caption'], sources=[dict(region='probe')], occurrences=[dict(interval=[0,1])]),
            dict(id='R1', text='The chair is blue.', qualification='source assertion',
                kinds=['caption'], sources=[dict(region='probe')], occurrences=[dict(interval=[1,2])]),
            dict(id='R2', text='C never uses a brush.', qualification='source assertion',
                kinds=['caption'], sources=[dict(region='probe')], occurrences=[dict(interval=[2,3])])])
        probe['pool_sha256'] = core.digest(probe['records'])
        question = dict(question='Which tool does C use to wipe the table?',
            options=['a cloth', 'a brush', 'a knife', 'a spoon', 'paper'])
        usage = generate(core.planner_input(question, probe, 'K-functional'),
            out / 'functional/planner_output.json', core.plan_schema(probe))
        checked = core.decode_plan(usage['raw'], probe)
        text, gate_audit = core.render(probe, checked)
        retained = set(checked['keep'] + checked['uncertain'] + [r for g in checked['merge_groups'] for r in g])
        passed = 'R0' in retained and 'R1' in checked['skip'] and 'C wipes the table using a cloth.' in text
        io.write(out / 'validation/functional_small.json', dict(passed=passed, benchmark_labels_used=False,
            plan=checked, audit=gate_audit, input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'],
            seconds=usage['seconds'], updated_at=io.now()))
        if not passed:
            raise ValueError('K synthetic evidence retention/filtering gate failed')

        # The old three-record gate did not exercise realistic partition sizes.
        # Fixed synthetic distractors, independent of benchmark questions or labels.
        large = deepcopy(probe)
        large['records'] = [deepcopy(probe['records'][0])]
        for i in range(1, 129):
            record = deepcopy(probe['records'][1])
            record.update(id=f'R{i}', text=f'Unused storage locker number {i} is blue.',
                occurrences=[dict(interval=[i,i+1])])
            large['records'].append(record)
        record = deepcopy(probe['records'][0])
        record.update(id='R129', text='After wiping, C puts the used cloth into the sink.',
            occurrences=[dict(interval=[129,130])])
        large['records'].append(record)
        large['pool_sha256'] = core.digest(large['records'])
        usage = generate(core.planner_input(question, large, 'K-functional-130'),
            out / 'functional/large_planner_output.json', core.plan_schema(large))
        checked = core.decode_plan(usage['raw'], large)
        _, audit = core.render(large, checked)
        retained = set(checked['keep'] + checked['uncertain'] + [r for g in checked['merge_groups'] for r in g])
        passed = 'R0' in retained and 'R1' in checked['skip'] and audit['total_records'] == 130
        io.write(out / 'validation/functional.json', dict(passed=passed, benchmark_labels_used=False,
            small_gate='validation/functional_small.json', candidate_records=130,
            plan=checked, audit=audit, input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'],
            seconds=usage['seconds'], updated_at=io.now()))
        if not passed:
            raise ValueError('K 130-record synthetic retention/filtering gate failed')

    if strict_reader:
        if hasattr(core, 'functional_probe'):
            probe_case = core.functional_probe(io.parts)
            usage = generate(core.planner_input(probe_case['question'], probe_case['pool'], label+'-parent-functional'),
                out / 'functional/parent_planner_output.json', core.plan_schema(probe_case['pool']))
            checked = core.decode_plan(usage['raw'], probe_case['pool'])
            text, audit = core.render(probe_case['pool'], checked)
            retained = set(checked['keep'] + checked['uncertain'] + [r for g in checked['merge_groups'] for r in g])
            passed = (set(probe_case['required_ids']) <= retained and
                set(probe_case['skipped_ids']) <= set(checked['skip']))
            io.write(out / 'validation/parent_memory_functional.json', dict(passed=passed,
                benchmark_labels_used=False, plan=checked, audit=audit, rendered=text, seconds=usage['seconds'], updated_at=io.now()))
            if not passed:
                raise ValueError(label+' parent-memory evidence functional gate failed')
        io.write(out / 'progress.json', dict(stage='reader_functional_gate', updated_at=io.now()))
        probe_options = ['cloth', 'brush', 'knife', 'spoon', 'paper']
        checks = []
        for index, arm in enumerate(arms + ('permuted_options',)):
            options = probe_options if index < 3 else ['brush', 'knife', 'spoon', 'cloth', 'paper']
            expected = 0 if index < 3 else 3
            options_text = '; '.join(chr(65+i) + '. ' + option for i, option in enumerate(options))
            body = 'C wipes the table using a cloth.'
            if index < 2:
                body += ' The chair is blue.'
            inputs = dict(query_id='K-reader-' + arm,
                head='Choose the option supported by the supplied observations. Use prediction: <letter>.',
                prompts=['Description: ' + body + '\nQuestion: Which tool does C use to wipe the table?\nOptions: ' + options_text])
            usage = generate(inputs, out / 'functional' / ('reader_' + arm + '_output.json'), answer_options=options)
            parsed = parse_strict_answer(usage['raw'], options)
            checks.append(dict(arm=arm, passed=parsed['option'] == expected, selected=parsed['option'],
                expected_synthetic_option=expected, seconds=usage['seconds']))
        passed = all(c['passed'] for c in checks)
        io.write(out / 'validation/reader_functional.json', dict(passed=passed, checks=checks,
            reader_protocol=READER_PROTOCOL, benchmark_labels_used=False, updated_at=io.now()))
        if not passed:
            raise ValueError('K constrained reader synthetic gate failed')

    def update_report():
        subprocess.run([sys.executable, str(Path(__file__).resolve()), 'report', '--out', str(out)],
            check=True, stdout=subprocess.DEVNULL)

    for index, uid in enumerate(io.read(out / 'cohort.json')):
        folder = out / 'runs' / uid
        if folder.exists():
            if uid not in policy.get('resume_saved_cases', {}):
                raise ValueError('K case already attempted; no automatic retries')
            if all((folder / a / 'result.json').exists() and not io.read(folder / a / 'result.json').get('error') for a in arms):
                continue
        else:
            io.write(folder / 'started.json', dict(updated_at=io.now(), pool_sha256=io.sha(out / 'pool' / (uid + '.json'))))
        pool = io.read(out / 'pool' / (uid + '.json'))
        question = io.read(out / 'question' / (uid + '.json'))
        io.write(out / 'progress.json', dict(stage='planning', index=index+1, question_id=uid, updated_at=io.now()))
        try:
            usage = generate(io.read(out / 'planner_input' / (uid + '.json')),
                folder / 'planner_output.json', core.plan_schema(pool))
            plan = core.decode_plan(usage['raw'], pool)
            description, audit = core.render(pool, plan)
            io.write(folder / 'plan.json', plan)
            io.write(folder / 'plan_audit.json', audit)
            all_input = io.read(out / arms[1] / (uid + '.json'))
            selected_input = core.final_input(all_input, description, io.parts)
            io.write(folder / selected_filename, selected_input)
            inputs = {arms[0]:io.read(out / arms[0] / (uid + '.json')),
                arms[1]:all_input, planned_arm:selected_input}
            # Rotate all three answer orders; all arms generate fresh responses.
            order = arms[index % len(arms):] + arms[:index % len(arms)]
            for arm in order:
                io.write(out / 'progress.json', dict(stage=arm, index=index+1, question_id=uid, updated_at=io.now()))
                arm_folder = folder / arm
                usage = generate(inputs[arm], arm_folder / 'answer_output.json', answer_options=question['options'])
                parsed = (parse_strict_answer if strict_reader else parse_final_answer)(usage['raw'], question['options'])
                result = dict(question_id=uid, selected_option=str(parsed['option']) if parsed['option'] is not None else None,
                    error=None if parsed['option'] is not None else dict(type='AnswerFormatError', message='Unresolved explicit answer'),
                    raw_response=usage['raw'], input_tokens=usage['input_tokens'], output_tokens=usage['output_tokens'],
                    seconds=usage['seconds'], input_sha256=core.digest(inputs[arm]), parser=parsed['method'])
                if strict_reader:
                    result['reader_protocol'] = READER_PROTOCOL
                result_path = arm_folder / 'result.json'
                if result_path.exists():
                    if io.read(result_path) != result:
                        raise ValueError('Inherited result differs from replay')
                else:
                    io.write(result_path, result)
                if result['error']:
                    raise ValueError('K answer format failed in ' + arm)
            update_report()
            print(label, index+1, uid, 'kept', audit['retained_records'], 'skipped', audit['skipped_records'], flush=True)
        except Exception as exc:
            io.write(folder / 'failure.json', dict(error_type=type(exc).__name__, message=str(exc), updated_at=io.now()))
            update_report()
            raise
    return 0


def queue(out):
    verify(out)
    policy = io.read(out / 'experiment.json')
    with (out / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out / 'launch.json').exists():
            raise ValueError('K model stage already attempted; no automatic retry')

        def status(state, **extra):
            io.write(out / 'queue_status.json', dict(state=state, controller_pid=os.getpid(), updated_at=io.now(), **extra))

        try:
            while not dependency_ready(policy):
                status('waiting_for_J', dependency=policy['after_queue'])
                time.sleep(30)
            from gpu_queue import acquire
            with acquire(policy['resource_policy'], lambda details: status('waiting_for_gpu', **details)) as (device, fds):
                env = dict(os.environ, CUDA_VISIBLE_DEVICES=device['uuid'], CUDA_DEVICE_ORDER='PCI_BUS_ID',
                    HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
                    OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', PYTHONUNBUFFERED='1')
                command = [sys.executable, '-u', str(Path(__file__).resolve()), 'worker', '--out', str(out)]
                with (out / 'worker.log').open('a') as log:
                    process = subprocess.Popen(command, cwd=out, env=env, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, pass_fds=fds)
                launch = dict(worker_pid=process.pid, gpu=device['index'], gpu_uuid=device['uuid'],
                    command=command, started_at=io.now())
                io.write(out / 'launch.json', launch)
                while process.poll() is None:
                    status('running', **launch)
                    time.sleep(5)
                summary = report(out)
                if process.returncode or not summary['complete']:
                    raise RuntimeError('K worker failed or cohort incomplete; exit=' + str(process.returncode))
                status('complete', **launch)
                return 0
        except Exception as exc:
            status('failed', error_type=type(exc).__name__, error=str(exc))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'prepare-resume', 'prepare-strict-reader', 'queue', 'worker', 'report'])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--after', type=Path)
    parser.add_argument('--prior', type=Path)
    parser.add_argument('--repair-empty-plan', action='store_true',
        help='Explicitly archive an all-skip failure and generate one new constrained plan')
    args = parser.parse_args()
    out = args.out.resolve()
    if args.mode == 'prepare':
        if args.parent is None:
            parser.error('prepare requires --parent F; --after is optional scheduling only')
        prepare(args.parent, out, args.after, prior=args.prior)
    elif args.mode == 'prepare-strict-reader':
        if args.prior is None:
            parser.error('prepare-strict-reader requires --prior failed K')
        prepare_strict_reader(args.prior, out)
    elif args.mode == 'prepare-resume':
        if args.prior is None:
            parser.error('prepare-resume requires --prior failed K')
        prepare_resume(args.prior, out, repair_empty_plan=args.repair_empty_plan)
    elif args.mode == 'queue':
        return queue(out)
    elif args.mode == 'worker':
        return worker(out)
    else:
        text = report(out)
        print(json.dumps(text, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
