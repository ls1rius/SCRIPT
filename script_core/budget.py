"""Serial leaf-call accounting with reservations and cancellable model workers."""
from datetime import datetime, timezone
import json
import multiprocessing as mp
from pathlib import Path
import time
import uuid

from .config import digest
from .accounting import metered_cpu


class ResourceExhausted(RuntimeError):
    pass


class ToolFailure(RuntimeError):
    pass


class ToolDeadlineExceeded(ToolFailure):
    """A measured deadline, with enough detail to distinguish budget exhaustion.

    A short allowance at the end of exploration can yield a partial graph. A
    timeout with a full per-call allowance remains an execution failure.
    """
    def __init__(self, message, *, operation=None, budget_limited=False,
                 worker_reusable=False):
        super().__init__(message)
        self.operation = operation
        self.budget_limited = budget_limited
        self.worker_reusable = worker_reusable


def exploration_exhausted(exc):
    return isinstance(exc, ResourceExhausted) or (
        isinstance(exc, ToolDeadlineExceeded) and exc.budget_limited and exc.worker_reusable)


class BudgetLedger:
    def __init__(self, total_cap, initialization_cost=0., answer_reserve=30., calls_path=None):
        self.total_cap = total_cap
        self.initialization_cost = initialization_cost
        self.answer_reserve = answer_reserve
        self.actual = 0.
        self.calls = []
        self.calls_path = Path(calls_path) if calls_path else None
        self.budget_violation = False
        self.dependency_cost = 0.
        self.dependency_keys = set()
        self.task_start = 0.
        self.task_cap = None
        self.warmup_reserve = 0.
        self.cleanup_reserve = 0.
        self.final_generation_start = None
        self.warmup_minimum = 30.
        if total_cap < initialization_cost + answer_reserve:
            raise ResourceExhausted('infeasible: initialization plus final answer reserve exceeds total cap')

    @property
    def remaining(self):
        return max(0., self.total_cap - self.initialization_cost - self.actual - self.dependency_cost - self.answer_reserve
                   - self.warmup_reserve - self.completion_cleanup)

    @property
    def completion_cleanup(self):
        if not self.cleanup_reserve:
            return 0.
        checkpoints = [c['actual_cost'] for c in self.calls if c['operation'] == 'checkpoint_write']
        # Keep room for one more state flush and the final export/inventory.
        return max(self.cleanup_reserve, 2 * max(checkpoints[-2:], default=0.) + 5.)

    def allowance(self, cap, final=False):
        available = self.total_cap-self.initialization_cost-self.actual-self.dependency_cost
        if not final:
            available -= self.answer_reserve + self.warmup_reserve + self.completion_cleanup
            if self.task_cap is not None:
                available = min(available, self.task_cap-(self.actual-self.task_start))
        return min(cap, max(0., available))

    def record(self, operation, inputs, reservation, started, output=None, error=None,
               task_id=None, call_id=None, model_hash=None, attempt=0):
        elapsed = time.monotonic()-started
        if elapsed > reservation + .05:
            self.budget_violation = True
        self.actual += elapsed
        if self.initialization_cost+self.actual+self.dependency_cost > self.total_cap+.05:
            self.budget_violation = True
        record = {
            'call_id': call_id or str(uuid.uuid4()), 'parent_task_id': task_id,
            'operation': operation, 'model_hash': model_hash, 'input_hash': digest(inputs),
            'actual_pts': [f['pts'] for f in inputs.get('frames', [])],
            'parameters': {k: (dict(uri=v['uri'], cutoff=v['cutoff'], media_hash=v['media_hash'],
                                     frame_catalog_hash=digest(v['frames'])) if k == 'manifest' else v)
                           for k, v in inputs.items() if k not in ('frames', 'images', 'features', 'vectors', 'detections')},
            'reservation': reservation, 'actual_cost': elapsed,
            'started_at': datetime.fromtimestamp(time.time()-elapsed, timezone.utc).isoformat(),
            'ended_at': datetime.now(timezone.utc).isoformat(), 'error': error,
            'output_hash': digest(output) if output is not None else None, 'attempt': attempt,
            'input_tokens': output.get('usage', {}).get('input_tokens', 0) if isinstance(output, dict) else 0,
            'output_tokens': output.get('usage', {}).get('output_tokens', 0) if isinstance(output, dict) else 0,
            'cpu_seconds': output.get('usage', {}).get('cpu_seconds') if isinstance(output, dict) else None,
            'gpu_resident_seconds': output.get('usage', {}).get('gpu_resident_seconds') if isinstance(output, dict) else None,
            'gpu_peak_allocated_bytes': output.get('usage', {}).get('gpu_peak_allocated_bytes') if isinstance(output, dict) else None,
            'replay': output.get('replay') if isinstance(output, dict) else None,
            'model_tensor_pixels': output.get('usage', {}).get('model_tensor_pixels', {}) if isinstance(output, dict) else {},
            'input_tokens_by_model': output.get('usage', {}).get('input_tokens_by_model', {}) if isinstance(output, dict) else {},
            'model_frame_views': output.get('usage', {}).get('model_frame_views', []) if isinstance(output, dict) else [],
            'context_projection': output.get('usage', {}).get('context_projection') if isinstance(output, dict) else None,
            'model_caption_context': output.get('usage', {}).get('model_caption_context') if isinstance(output, dict) else None,
            'input_pixels': sum(f.get('width', 0)*f.get('height', 0) for f in inputs.get('frames', [])),
            'output_ref': str(self.calls_path.parent / 'raw_calls' / f'{call_id}-{attempt}.json') if self.calls_path else None,
        }
        if inputs.get('in_process'):
            record['output_ref'] = None
        if isinstance(output, dict) and output.get('usage', {}).get('tool_cache'):
            record['tool_cache'] = dict(output['usage']['tool_cache'])
            cache = record['tool_cache']
            charged = cache['source_seconds'] if cache['hit'] and cache['key'] not in self.dependency_keys else 0.
            self.dependency_keys.add(cache['key'])
            self.dependency_cost += charged
            cache['budget_dependency_seconds'] = charged
            if self.initialization_cost+self.actual+self.dependency_cost > self.total_cap+.05:
                self.budget_violation = True
        self.calls.append(record)
        if self.calls_path:
            self.calls_path.parent.mkdir(parents=True, exist_ok=True)
            with self.calls_path.open('a') as f:
                f.write(json.dumps(record, ensure_ascii=False)+'\n')
        return record

    def summary(self):
        groups = {}
        for c in self.calls:
            groups[c['operation']] = groups.get(c['operation'], 0.) + c['actual_cost']
        return {'unit': 'processing_seconds', 'initialization': self.initialization_cost,
                'online': self.actual, 'total': self.initialization_cost+self.actual,
                'shared_dependency_seconds': self.dependency_cost,
                'budgeted_total': self.initialization_cost+self.actual+self.dependency_cost,
                'total_cap': self.total_cap, 'budget_violation': self.budget_violation,
                'by_operation': groups, 'call_count': len(self.calls), 'resources': self.resources()}
        # Kept below as a separate public method so resumed historical logs remain readable.

    def resources(self):
        return {'cpu_seconds': sum(c.get('cpu_seconds') or 0. for c in self.calls),
                'cpu_unmeasured_calls': sum(c.get('cpu_seconds') is None for c in self.calls),
                'gpu_resident_seconds': sum(c.get('gpu_resident_seconds') or 0. for c in self.calls),
                'gpu_unmeasured_calls': sum(c.get('gpu_resident_seconds') is None for c in self.calls),
                'gpu_peak_allocated_bytes': max((c.get('gpu_peak_allocated_bytes') or 0 for c in self.calls), default=0)}


def _worker(connection, cfg):
    from .backend import LocalModels
    backend = LocalModels(cfg)
    gpu_reported_until = None
    while True:
        message = connection.recv()
        if message is None:
            return
        operation, inputs, deadline = message
        backend.deadline = deadline
        backend.model_pixels, backend.model_tokens = {}, {}
        began, cpu_began = time.monotonic(), time.process_time()
        try:
            result = backend.call(operation, inputs)
            usage = dict(result.get('usage', {}))
            usage.update(model_tensor_pixels=backend.model_pixels, input_tokens_by_model=backend.model_tokens)
            usage['cpu_seconds'] = time.process_time()-cpu_began
            import torch
            gpu = cfg['models']['device'] == 'cuda' and torch.cuda.is_initialized()
            if gpu:
                torch.cuda.synchronize()
            gpu_end = time.monotonic()
            usage['gpu_resident_seconds'] = gpu_end-(gpu_reported_until or backend.gpu_acquired_at or began) if gpu else 0.
            if gpu:
                gpu_reported_until = gpu_end
            usage['gpu_peak_allocated_bytes'] = torch.cuda.max_memory_allocated() if gpu else 0
            result['usage'] = usage
            connection.send((True, result))
        except Exception as exc:
            from .backend import SoftDeadlineExceeded
            connection.send((False, {'message': f'{type(exc).__name__}: {exc}',
                                    'soft_timeout': isinstance(exc, SoftDeadlineExceeded),
                                    'raw_output':getattr(exc,'raw_output',None),
                                    'usage': {'cpu_seconds': time.process_time()-cpu_began}}))


class ModelWorker:
    """Persistent model process. A hard deadline kills it, including CUDA work."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.connection = None
        self.vlm_ready = False

    @property
    def requires_vlm_warmup(self):
        return not self.vlm_ready or self.process is None or not self.process.is_alive()

    def start(self):
        if self.process is not None and self.process.is_alive():
            return
        ctx = mp.get_context('spawn')
        self.connection, child = ctx.Pipe()
        self.process = ctx.Process(target=_worker, args=(child, self.cfg), daemon=True)
        self.process.start()
        child.close()

    def call(self, operation, inputs, timeout):
        began = time.monotonic()
        self.start()
        # Stop between generated tokens/batches, then retain the loaded models.
        # The existing parent timeout is still the hard cancellation fallback.
        deadline = began + max(0., timeout-min(2., timeout/5))
        self.connection.send((operation, inputs, deadline))
        if not self.connection.poll(max(0., timeout-(time.monotonic()-began))):
            self.close()
            raise TimeoutError(f'{operation} exceeded reserved {timeout:.2f}s')
        ok, value = self.connection.recv()
        if not ok:
            if value.get('soft_timeout'):
                exc = ToolDeadlineExceeded(value['message'], operation=operation,
                                           worker_reusable=True)
            else:
                exc = ToolFailure(value['message'])
            exc.usage = value['usage']
            exc.raw_output=value.get('raw_output')
            raise exc
        if operation == 'prepare_vlm':
            self.vlm_ready = True
        return value

    def close(self, graceful=False):
        self.vlm_ready = False
        if self.process is not None:
            if graceful and self.process.is_alive():
                try:
                    self.connection.send(None)
                    self.process.join(timeout=10)
                except (OSError, EOFError):
                    pass
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join()
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class Meter:
    def __init__(self, backend, ledger, cfg, raw_directory):
        self.backend, self.ledger, self.cfg = backend, ledger, cfg
        self.raw_directory = Path(raw_directory)
        self.raw_directory.mkdir(parents=True, exist_ok=True)
        from .accounting import ACTIVE
        self.active_token = ACTIVE.set(self)
        self.active_task_id = None
        self.closed = False

    def close(self):
        from .accounting import ACTIVE
        if not self.closed:
            ACTIVE.reset(self.active_token)
            self.closed = True

    def call(self, operation, inputs, task_id=None, final=False, build=False):
        from .accounting import tool_boundary
        with tool_boundary():
            return self._call(operation, inputs, task_id, final, build)

    def _call(self, operation, inputs, task_id=None, final=False, build=False):
        cap = self.cfg['budget']['build_call_cap' if build else 'tool_call_cap']
        if final and operation != 'prepare_vlm':
            cap = self.cfg['budget']['final_answer_reserve']
        call_id = str(uuid.uuid4())
        retries = self.cfg['query']['tool_transient_retries']
        for attempt in range(retries+1):
            from .prompts import CONTRACTS
            if isinstance(self.backend, ModelWorker) and operation != 'prepare_vlm':
                available = self.ledger.allowance(cap, final=final)
                if available < self.expected_cost(operation):
                    raise ResourceExhausted(f'Insufficient allowance to finish {operation}; call skipped')
            if operation in CONTRACTS and getattr(self.backend, 'requires_vlm_warmup', False) is True:
                # Cold model/tokenizer setup has its own measured leaf call. It
                # consumes the same online allowance, without a generation timeout.
                self.call('prepare_vlm', {}, final=final, build=True)
            allowance = self.ledger.allowance(cap, final=final)
            if operation == 'prepare_vlm':
                # Preparation is a separately paid stage, not a generation call.
                # It may use its own held reserve while preserving answer/cleanup
                # time and, inside a task, the original local task cap.
                available = (self.ledger.total_cap-self.ledger.initialization_cost-self.ledger.actual-self.ledger.dependency_cost
                             -self.ledger.answer_reserve-self.ledger.completion_cleanup)
                if not final and self.ledger.task_cap is not None:
                    available = min(available, self.ledger.task_cap-(self.ledger.actual-self.ledger.task_start))
                allowance = min(cap, max(0., available))
            if final:
                available = self.ledger.total_cap-self.ledger.initialization_cost-self.ledger.actual-self.ledger.dependency_cost-self.ledger.completion_cleanup
                if operation == 'prepare_vlm':
                    available -= self.ledger.answer_reserve
                else:
                    if self.ledger.final_generation_start is None:
                        self.ledger.final_generation_start = self.ledger.actual
                    available = min(available, self.ledger.answer_reserve-(self.ledger.actual-self.ledger.final_generation_start))
                allowance = min(allowance, max(0., available))
            # Do not restart a cold CUDA worker with a fraction of a second left.
            if operation == 'prepare_vlm' and allowance < self.ledger.warmup_minimum:
                raise ResourceExhausted('Insufficient local allowance for model preparation')
            if isinstance(self.backend, ModelWorker) and allowance < 4.:
                raise ResourceExhausted('Insufficient allowance for worker call and cancellation')
            if isinstance(self.backend, ModelWorker) and operation != 'prepare_vlm' and allowance < self.expected_cost(operation):
                raise ResourceExhausted(f'Insufficient allowance to finish {operation}; call skipped')
            if allowance < .001:
                raise ResourceExhausted('No processing budget available')
            started = time.monotonic()
            parent_cpu = time.process_time()
            output, error, failure = None, None, None
            from .state import write_json
            pending = self.raw_directory / f'{call_id}-{attempt}.pending.json'
            write_json(pending, {'call_id': call_id, 'attempt': attempt, 'reservation': allowance,
                                 'operation': operation, 'input_hash': digest(inputs), 'parent_task_id': task_id})
            try:
                # Worker cancellation and durable call logs are also paid time.
                # Leave room for them rather than starting shutdown at the deadline.
                timeout = allowance-(time.monotonic()-started)-min(8., allowance/2)
                if timeout <= .001:
                    raise ResourceExhausted('No tool budget remains after staging and cleanup reserve')
                output = self.backend.call(operation, inputs, timeout)
            except Exception as exc:
                failure = exc
                error = f'{type(exc).__name__}: {exc}'
                if getattr(exc, 'usage', None):
                    output = dict(getattr(exc,'raw_output',None) or {})
                    output['usage']=dict(output.get('usage',{}),**exc.usage)
            if isinstance(output, dict):
                usage = output.setdefault('usage', {})
                usage['cpu_seconds'] = (usage.get('cpu_seconds') or 0.)+time.process_time()-parent_cpu
            path = self.raw_directory / f'{call_id}-{attempt}.json'
            write_json(path, {'inputs': inputs, 'output': output, 'error': error})
            call = self.ledger.record(operation, inputs, allowance, started, output, error,
                                      task_id, call_id, digest(self.cfg['models']), attempt)
            call['output_ref'] = str(path)
            pending.unlink()
            if error is None:
                if operation == 'prepare_vlm':
                    self.ledger.warmup_minimum = max(30., call['actual_cost']*1.25+8.)
                    if self.ledger.warmup_reserve:
                        online_cap = self.ledger.total_cap-self.ledger.initialization_cost
                        self.ledger.warmup_reserve = max(self.ledger.warmup_reserve,
                            min(online_cap/4, self.ledger.warmup_minimum))
                return output, call
            if isinstance(failure, ToolDeadlineExceeded):
                raise ToolDeadlineExceeded(error, operation=operation,
                    budget_limited=not final and allowance < cap-.05,
                    worker_reusable=failure.worker_reusable) from failure
            if not error.startswith(('TimeoutError:', 'ConnectionError:', 'EOFError:')) or attempt == retries:
                raise ToolFailure(error)
        raise ToolFailure('Retry limit exceeded')

    def expected_cost(self, operation):
        """Measured stage cost including staging/cancellation room, not extra budget."""
        import statistics
        defaults = {'observe': 4., 'extract': 12., 'assess': 6., 'detect': 6.,
                    'verify_claim': 3., 'verify_claims': 7., 'verify_identity': 4., 'coverage': 5., 'goal': 4., 'answer': 3.}
        samples = [c['actual_cost'] for c in self.ledger.calls
                   if c['operation'] == operation and not c.get('error')][-3:]
        processing = statistics.median(samples) if samples else defaults.get(operation, 1.)
        # Include the existing 8s cancellation/log reserve and 2s soft deadline
        # margin, otherwise a "fitting" generation would have too little runtime.
        return max(12., processing*1.25+10.)

    @metered_cpu('structured_output_validation')
    def json(self, operation, inputs, required, task_id=None, final=False, build=False):
        if operation == 'assess':
            from .context import assessment_inputs
            inputs = assessment_inputs(inputs)
        try:
            output, call = self.call(operation, inputs, task_id, final, build)
        except ToolFailure as exc:
            if operation != 'assess' or 'Context overflow' not in str(exc) or len(inputs.get('claims', [])) < 2:
                raise
            # Independent assessments retain every fact and its references. Never
            # silently truncate a growing history to make it fit the context.
            reports = []
            middle = len(inputs['claims'])//2
            for claims in (inputs['claims'][:middle], inputs['claims'][middle:]):
                part = dict(inputs, claims=claims)
                reports.append(self.json(operation, part, required, task_id, final, build)[0])
            fields = set().union(*(r.keys() for r in reports))
            return {field: [item for r in reports for item in r.get(field, [])] for field in fields}, None
        parse_context = {}
        for attempt in range(self.cfg['query']['json_repair_retries']+1):
            try:
                value = output['data']
                if isinstance(value, str):
                    text = value.strip()
                    if text.startswith('```'):
                        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
                    value = json.loads(text,strict=False)
                if isinstance(value, dict) and set(value) == {'data'} and isinstance(value['data'], dict):
                    value = value['data']
                if operation == 'parse' and isinstance(value, dict):
                    from .query_policy import retain_plan_context
                    value = retain_plan_context(value, parse_context)
                    parse_context = value
                from .schemas import output_schema, validate_output, restore_empty_arrays
                schema = output_schema(operation, inputs)
                value = restore_empty_arrays(value, schema)
                if not isinstance(value, dict) or set(required)-set(value):
                    raise ValueError(f'Required fields: {required}')
                origin = value.pop('_replay_origin', None) if output.get('replay') else None
                validate_output(value, schema)
                if origin is not None:
                    value['_replay_origin'] = origin
                return value, call
            except (KeyError, TypeError, ValueError) as exc:
                if attempt == self.cfg['query']['json_repair_retries']:
                    raise ToolFailure(f'Invalid {operation} JSON: {exc}') from exc
                if operation in ('scene_scope','temporal_review','caption_temporal_observe'):
                    # Regenerate from canonical input IDs. Copying a malformed
                    # aliased output into repair can preserve broken references
                    # and repeats the same overlong free-text continuation.
                    inputs=dict(inputs,validation_feedback=dict(error=str(exc),
                        instruction='Regenerate valid complete JSON from the original inputs. '
                        'Keep explanations under 60 characters. Do not copy the rejected output.'))
                    if operation=='caption_temporal_observe':
                        inputs.update(page_size=1,max_observations=3)
                    output,call=self.call(operation,inputs,task_id,final,build)
                    continue
                output, call = self.call('repair', {'original_operation': operation,
                                                   'original_output': output, 'errors': str(exc),
                                                   'original_inputs': inputs,
                                                   'required_fields': list(required)}, task_id, final, build)
