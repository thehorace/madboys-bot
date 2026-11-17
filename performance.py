"""Bounded, process-local timings. Labels never contain player names or SQL."""
import functools
import inspect
import logging
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager

_samples = defaultdict(lambda: deque(maxlen=120))
_lock = threading.Lock()
log = logging.getLogger('madboys-bot.performance')

@contextmanager
def measure(label, slow=1.0):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter()-start
        with _lock:
            _samples[label].append(elapsed)
        if elapsed >= slow:
            log.warning('%s took %.3fs', label, elapsed)

def timed(label, slow=1.0):
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                with measure(label, slow):
                    return await fn(*args, **kwargs)
            return async_wrapper
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with measure(label, slow):
                return fn(*args, **kwargs)
        return wrapper
    return decorate

def snapshot():
    with _lock:
        return {k: {'count':len(v),'mean':sum(v)/len(v),'max':max(v)} for k,v in _samples.items() if v}
