"""Bound pipe memory while draining both streams and owning timeout/cancellation."""
from __future__ import annotations
import subprocess
import threading
import time


class OutputLimit(ValueError):
    pass


class ProcessCancelled(ValueError):
    pass


def collect_output(proc, *, timeout, limit, terminate, input=None,
                   cancel_event=None, reject_overflow=False):
    buffers = ["", ""]
    truncated = [False, False]
    failure = []
    overflow = threading.Event()

    def reader(index, pipe):
        try:
            while chunk := pipe.read(4096):
                value = buffers[index] + chunk
                truncated[index] |= len(value) > limit
                buffers[index] = value[-limit:]
                if reject_overflow and truncated[index]:
                    overflow.set()
        except (OSError, UnicodeError) as exc:
            failure.append(exc)
        finally:
            pipe.close()

    def writer():
        try:
            if input is not None:
                proc.stdin.write(input)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            proc.stdin.close()

    threads = [threading.Thread(target=reader, args=(index, pipe), daemon=True)
               for index, pipe in enumerate((proc.stdout, proc.stderr))]
    if proc.stdin is not None:
        threads.append(threading.Thread(target=writer, daemon=True))
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None or any(thread.is_alive() for thread in threads):
            if proc.poll() is not None and getattr(proc, "_gitgo_windows_scope", None) is not None:
                from backend.core.process_control import finish_scope
                finish_scope(proc)
            if cancel_event is not None and cancel_event.is_set():
                raise ProcessCancelled("process cancelled")
            if overflow.is_set():
                raise OutputLimit(f"PROCESS_OUTPUT_LIMIT: pipe exceeded {limit} characters")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            time.sleep(0.02)
        if overflow.is_set():
            raise OutputLimit(f"PROCESS_OUTPUT_LIMIT: pipe exceeded {limit} characters")
        if failure:
            raise failure[0]
        from backend.core.process_control import finish_scope
        finish_scope(proc)
        return buffers[0], buffers[1], any(truncated)
    except BaseException:
        terminate()
        for thread in threads:
            thread.join(timeout=1)
        raise
