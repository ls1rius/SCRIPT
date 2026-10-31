"""H source-only microbatching; independent schemas, caches and F-style execution."""
from collections import Counter
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

BATCH_OPERATION = 'h_extract_batch'
EXECUTION_VERSION = 'H-independent-source-batch-v1'


def batch_module():
    try:
        return importlib.import_module('batch_extraction')
    except ModuleNotFoundError as exc:
        if exc.name != 'batch_extraction':
            raise
        return importlib.import_module('script_core.batch_extraction')


def runtime_modules():
    return SimpleNamespace(**{name: importlib.import_module('script_core.' + module)
        for name, module in dict(backend='backend', memory='generalized_lazy_memory',
            context='context', schemas='schemas', prompts='prompts').items()})


def batched_worker(connection, cfg):
    """Reuse the original worker's deadlines, resource accounting and error handling."""
    runtime = runtime_modules()
    original = runtime.backend.LocalModels
    batch = batch_module()

    class LocalModelsWithBatch(original):
        def call(self, operation, inputs):
            if operation != BATCH_OPERATION:
                return super().call(operation, inputs)
            if set(inputs) != {'requests'}:
                raise ValueError('Only independent source extraction requests are supported')
            outputs = batch.BatchExtractor(self.cfg, runtime, models=self).generate(inputs['requests'])
            usage = {key: sum(row['usage'][key] for row in outputs)
                for key in ('input_tokens', 'output_tokens')}
            self.model_tokens['vlm'] = usage['input_tokens']
            return dict(items=outputs, usage=usage)

    runtime.backend.LocalModels = LocalModelsWithBatch
    importlib.import_module('script_core.budget')._worker(connection, cfg)


def install_worker():
    # ModelWorker.start resolves this module-level target when spawning its child.
    importlib.import_module('script_core.budget')._worker = batched_worker


def restore_ledger(base):
    """Resume paid calls without resetting the current question's allowance."""
    class Ledger(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if not self.calls_path or not self.calls_path.exists():
                return
            self.calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
            self.actual = sum(row['actual_cost'] for row in self.calls)
            self.budget_violation = any(row['actual_cost'] > row['reservation'] + .05
                for row in self.calls) or self.actual + self.initialization_cost > self.total_cap + .05
            for row in self.calls:
                cache = row.get('tool_cache') or {}
                if cache.get('hit') and cache['key'] not in self.dependency_keys:
                    self.dependency_cost += cache.get('budget_dependency_seconds', cache.get('source_seconds', 0))
                if cache:
                    self.dependency_keys.add(cache['key'])
    return Ledger


class SourceExtractor:
    def __init__(self, meter, core, protocol, serial, batch_size=2):
        if batch_size not in (1, 2, 4):
            raise ValueError('Source batch size must be 1, 2 or 4')
        self.meter, self.core, self.protocol, self.serial = meter, core, protocol, serial
        self.batch_size = batch_size
        self.runtime = SimpleNamespace(memory=core,
            backend=importlib.import_module(core.__package__ + '.backend'),
            schemas=importlib.import_module(core.__package__ + '.schemas'))
        self.recovered = {}
        self.recovered_sources = []
        # A completed call may have been logged immediately before shutdown but
        # not yet committed to SQLite. Reuse its source-only output once.
        for call in meter.ledger.calls:
            if call.get('error') or call['operation'] != core.EXTRACT:
                continue
            path = Path(call.get('output_ref') or '')
            if not path.is_file():
                continue
            raw = json.loads(path.read_text())
            if raw.get('error') is None and raw.get('output'):
                self.recovered[core.digest(raw['inputs'])] = raw['output']
                self.recovered_sources.append((raw['inputs'], raw['output']))

    def inputs(self, unit, fields):
        return dict(unit=dict(id=unit['id'], text=self.core.joint.clean_unit(unit)['text']),
            requested_fields=fields, source_type='caption')

    def guarded(self, unit, fields):
        text = self.core.joint.clean_unit(unit)['text']
        if self.core.joint.NEGATION.search(text) or self.core.joint.UNCERTAINTY.search(text):
            return True
        schema = self.core.output_schema(self.core.EXTRACT, self.inputs(unit, fields))
        return all(schema['properties'][category]['maxItems'] == 0
            for category in self.core.joint.COLLECTIONS.values())

    def inspect(self, unit, inputs, output):
        result = batch_module().inspect_output(inputs, output, self.runtime)
        if result['error']:
            return dict(accepted=dict(facts=[], mentions=[], rejected=[], uncertain=True),
                error=dict(type='ToolFailure', message=result['error']), model_called=True)
        # Keep the exact original F acceptance boundary, including source markers.
        return dict(accepted=self.core.joint.accept(unit, result['data']), error=None, model_called=True)

    def __call__(self, unit, fields):
        inputs = self.inputs(unit, fields)
        previous = self.recovery(inputs)
        if previous is not None:
            original_inputs, output = previous
            return self.inspect(unit, original_inputs, output)
        return self.serial(unit, fields, self.meter, self.core, self.protocol)

    def recovery(self, inputs):
        for original, output in reversed(self.recovered_sources):
            if (original['unit'] == inputs['unit'] and original['source_type'] == inputs['source_type']
                    and set(inputs['requested_fields']) <= set(original['requested_fields'])):
                return original, output
        return None

    def many(self, jobs):
        accounting = importlib.import_module(self.core.__package__ + '.accounting')
        with accounting.cpu_phase('f_batch_preparation'):
            return self._many(jobs)

    def _many(self, jobs):
        if not jobs or len(jobs) > self.batch_size:
            raise ValueError('Unexpected source batch size')
        results, pending = {}, []
        for unit, fields in jobs:
            inputs = self.inputs(unit, fields)
            if self.guarded(unit, fields) or self.recovery(inputs) is not None:
                results[unit['id']] = self(unit, fields)
            else:
                pending.append((unit, fields, inputs))
        if len(pending) == 1:
            unit, fields, _ = pending[0]
            results[unit['id']] = self(unit, fields)
        elif pending:
            try:
                output, _ = self.meter.call(BATCH_OPERATION,
                    dict(requests=[inputs for _, _, inputs in pending]))
                if len(output['items']) != len(pending):
                    raise ValueError('Batch response count does not match source requests')
            except Exception as exc:
                budget_limited = type(exc).__name__ == 'ResourceExhausted' or getattr(exc, 'budget_limited', False)
                if self.protocol.is_infrastructure(exc) and not budget_limited:
                    raise
                for unit, _, _ in pending:
                    results[unit['id']] = dict(accepted=dict(facts=[], mentions=[], rejected=[], uncertain=True),
                        error=dict(type=type(exc).__name__, message=str(exc)), model_called=True)
            else:
                for (unit, _, inputs), item in zip(pending, output['items']):
                    results[unit['id']] = self.inspect(unit, inputs, item)
        return results


def ensure_batched(store, video, units, rows, fields, extract, progress=None):
    """Prefetch a bounded group, then commit using the H memory code."""
    mapping = store.register(video, units, rows)
    results, stats = {}, Counter()
    for start in range(0, len(units), extract.batch_size):
        chunk = units[start:start + extract.batch_size]
        jobs, seen = [], set()
        for unit in chunk:
            key = mapping[unit['id']]
            missing = [field for field in fields if store.get(video, key, field) is None]
            if missing and key not in seen:
                jobs.append((unit, missing))
                seen.add(key)
        if progress and jobs:
            progress(start, jobs[0][0], jobs[0][1])
        values = extract.many(jobs) if jobs else {}
        def ready(unit, missing):
            if unit['id'] not in values:
                raise ValueError('Source cache changed after batch preparation')
            return values[unit['id']]
        ids = {unit['id'] for unit in chunk}
        callback = (lambda i, unit, missing: progress(start + i, unit, missing)) if progress else None
        part, counts = store.ensure(video, chunk, [row for row in rows if row['source_id'] in ids],
            fields, ready, callback)
        results.update(part)
        stats.update(counts)
    return results, dict(stats)


def extraction_counts(calls, operation):
    single = sum(row['operation'] == operation for row in calls)
    batches = [row for row in calls if row['operation'] == BATCH_OPERATION]
    return dict(field_extraction_model_calls=single + len(batches),
        field_extraction_source_requests=single + sum(len(row['parameters']['requests']) for row in batches),
        extraction_batches=len(batches))
