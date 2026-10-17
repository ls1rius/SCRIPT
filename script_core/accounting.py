"""Non-overlapping CPU phases, including cache/graph/SQLite work between model calls."""
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import time

ACTIVE = ContextVar('script_meter', default=None)
STACK = ContextVar('script_cpu_stack', default=())


class Span:
    def __init__(self, meter, operation, final=False):
        self.meter, self.operation = meter, operation
        self.final = final
        self.wall = self.cpu = 0.
        self.started = self.cpu_started = None
        self.reservation = meter.ledger.allowance(meter.cfg['budget']['build_call_cap'], final=final)
        self.resume()

    def pause(self):
        if self.started is not None:
            self.wall += time.monotonic()-self.started
            self.cpu += time.process_time()-self.cpu_started
            self.started = None

    def resume(self):
        self.started, self.cpu_started = time.monotonic(), time.process_time()

    def settle(self):
        self.pause()
        if self.wall:
            self.meter.ledger.record(self.operation, {'in_process': True}, self.reservation,
                time.monotonic()-self.wall, output={'usage': {'cpu_seconds': self.cpu,
                    'gpu_resident_seconds': 0.}}, task_id=getattr(self.meter, 'active_task_id', None))
            self.wall = self.cpu = 0.
        self.reservation = self.meter.ledger.allowance(self.meter.cfg['budget']['build_call_cap'], final=self.final)


@contextmanager
def cpu_phase(operation, final=False):
    meter, stack = ACTIVE.get(), STACK.get()
    if meter is None:
        yield
        return
    if stack:
        stack[-1].pause()
    # A checkpoint consumes the reserved completion overhead even when it is a
    # periodic flush. Its nested validation must use the same reservation.
    span = Span(meter, operation, final or operation == 'checkpoint_write' or bool(stack and stack[-1].final))
    token = STACK.set(stack+(span,))
    try:
        yield
    finally:
        # Ledger serialization is bookkeeping, excluded to avoid recursively timing its own records.
        active_token = ACTIVE.set(None)
        try:
            span.settle()
        finally:
            ACTIVE.reset(active_token)
            STACK.reset(token)
            if stack:
                stack[-1].resume()


def metered_cpu(operation):
    def decorate(function):
        import inspect
        signature = inspect.signature(function)
        @wraps(function)
        def wrapped(*args, **kwargs):
            final = signature.bind_partial(*args, **kwargs).arguments.get('final', False)
            with cpu_phase(operation, final=final):
                return function(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def tool_boundary():
    """Settle CPU phases before reserving a leaf call; never count the enclosing flow twice."""
    stack = STACK.get()
    for span in stack:
        span.pause()
    token = ACTIVE.set(None)
    try:
        for span in reversed(stack):
            span.settle()
        yield
    finally:
        ACTIVE.reset(token)
        if stack:
            stack[-1].reservation = stack[-1].meter.ledger.allowance(stack[-1].meter.cfg['budget']['build_call_cap'], final=stack[-1].final)
            stack[-1].resume()


@metered_cpu('storage_inventory')
def directory_stats(directory, final=False):
    total = count = 0
    for path in Path(directory).rglob('*'):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
            count += 1
    return {'bytes': total, 'files': count}
