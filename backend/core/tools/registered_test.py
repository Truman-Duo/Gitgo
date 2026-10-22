"""Native pytest producer; no access to authoritative test state."""
from pathlib import Path
import subprocess
import sys
import time


def execute_test_seeds(workspace, args, validation=None):
    workspace = Path(workspace).resolve()
    target = str(args.get("target") or "")
    path = (workspace / target.split("::", 1)[0]).resolve()
    try:
        path.relative_to(workspace)
    except ValueError:
        return {"error": "test target escapes workspace"}
    if not target or target.startswith("-") or not path.is_file():
        return {"error": "test target must be an existing pytest file/node"}
    seeds = args.get("seeds", [42]) or [42]
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= 20 or any(type(s) is not int for s in seeds):
        return {"error": "seeds must contain between 1 and 20 integers"}
    timeout = max(1, min(int(args.get("timeout", 120)), 900))
    from backend.core.tools.execution_profile import safe_environment
    from backend.core.child_process import workspace_python_command
    results = []
    for seed in seeds:
        env = safe_environment()
        env["GITGO_TEST_SEED"] = str(seed)
        started = time.monotonic()
        try:
            # AppContainer cannot open the Windows NUL device. Pytest's
            # supported sys capture avoids that fd-capture dependency while
            # the outer native runner still owns the real stdout/stderr pipes.
            capture = []
            if sys.platform == "win32":
                # Keep logs inside the already private execution view. Default
                # inherited creation permissions preserve its profile access;
                # Python's Windows mode-0700 temporary directories do not.
                (workspace / ".pytest_cache").mkdir(exist_ok=True)
                capture = ["-p", "backend.core.tools.sandbox_pytest", "--capture=sys", "--log-file=.pytest_cache/gitgo-seed-" + str(seed) + ".log"]
            arguments = ["python", "-m", "pytest", target, "-q", "--rootdir=.", "--confcutdir=.", *capture]
            if sys.platform == "win32" and not getattr(sys, "frozen", False):
                # The invocation-only plugin belongs to this trusted runtime,
                # not the project or the child's ambient Python search path.
                # Embedded Python can ignore cwd/PYTHONPATH or name another
                # checkout in ._pth. Bind the producer's own source explicitly;
                # preserve project imports through workspace_python_command.
                bootstrap = ("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
                             "runpy.run_module('pytest',run_name='__main__',alter_sys=True)")
                arguments = ["python", "-c", bootstrap, str(Path(__file__).resolve().parents[3]), *arguments[3:]]
            completed = subprocess.run(workspace_python_command(arguments, workspace),
                cwd=str(workspace), env=env, capture_output=True, text=True, encoding="utf8", errors="replace",
                timeout=timeout, shell=False)
            output = (completed.stdout or "") + (completed.stderr or "")
            code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            def text(value):
                return value.decode("utf8", errors="replace") if isinstance(value, bytes) else value or ""
            output, code = text(exc.stdout) + text(exc.stderr), 124
        results.append({"seed": seed, "exit_code": code, "duration_ms": (time.monotonic()-started)*1000,
                        "output_tail": output[-4000:]})
    return {"seed_results": results}
