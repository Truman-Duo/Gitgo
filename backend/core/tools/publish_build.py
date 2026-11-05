"""Closed native-only builder: one fixed Python entry, declared output files."""
from contextlib import chdir, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import runpy
import sys

from backend.core.publish.package import recipe


class _Tail(io.TextIOBase):
    def __init__(self):
        self.value = ""

    def write(self, value):
        self.value = (self.value + str(value))[-8000:]
        return len(value)

    def flush(self):
        pass


def run_publish_build(args):
    # The Host supplies this canonical private path. Ancestor listing/realpath
    # is intentionally unavailable inside AppContainer.
    root = Path(args["_workspace"])
    declared = recipe(args["recipe"])
    output = _Tail()
    previous_path, previous_argv = list(sys.path), list(sys.argv)
    try:
        sys.path.insert(0, str(root))
        sys.argv = [str(root / declared["entry"])]
        with chdir(root), redirect_stdout(output), redirect_stderr(output):
            try:
                runpy.run_path(str(root / declared["entry"]), run_name="__main__")
            except SystemExit as exc:
                if exc.code not in (None, 0):
                    return {"passed": False, "error": "PUBLISH_BUILD_FAILED", "output_tail": output.value}
        # Bytes are read by the parent from the retired private view. Child
        # JSON cannot forge file digests or exceed the bounded protocol pipe.
        return {"passed": True, "outputs": declared["outputs"], "output_tail": output.value}
    except Exception as exc:
        return {"passed": False, "error": "PUBLISH_BUILD_FAILED", "error_type": type(exc).__name__, "output_tail": output.value}
    finally:
        sys.path[:], sys.argv = previous_path, previous_argv
