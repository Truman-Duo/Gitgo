"""Real installed Shell discovery and execution; no configured Bash path."""

import sys as _development_sys
from pathlib import Path as _DevelopmentPath
_development_sys.path.insert(0, str(_DevelopmentPath(__file__).resolve().parents[1]))
from scripts.development_paths import development_root, test_state_home
from pathlib import Path
import json
import os
import sys
import uuid
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.tools.workspace_tools import shell_script, _resolve_bash
from backend.core.tools.execution_profile import powershell_profile


def main():
    root = development_root(ROOT) / "runtime-hardening" / ("shell-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    saved = {k: os.environ.get(k) for k in ("GITGO_BASH_PATH", "GITGO_TEST_SECRET", "BASH_ENV")}
    try:
        os.environ.pop("GITGO_BASH_PATH", None)
        os.environ["GITGO_TEST_SECRET"] = "test-value-never-inherit"
        injection = root / "injection.sh"
        injection.write_text("touch INJECTION_RAN\n")
        os.environ["BASH_ENV"] = str(injection)
        profiles = {"bash": _resolve_bash(), "powershell": powershell_profile()}
        results = {}
        for shell, script in {"bash": 'test -z "$GITGO_TEST_SECRET" && printf "BASH_PROFILE_OK"',
                "powershell": "if ($env:GITGO_TEST_SECRET) {exit 9}; [Console]::Write('POWERSHELL_PROFILE_OK')"}.items():
            result = shell_script({"_workspace": str(root), "shell": shell, "script": script,
                                   "purpose": "verify installed shell and sanitized environment", "timeout": 20})
            assert result.get("success"), result
            assert result["stdout"] == shell.upper() + "_PROFILE_OK", result
            results[shell] = {"success": True, "path": profiles[shell]["path"], "stdout": result["stdout"]}
        assert not (root / "INJECTION_RAN").exists()
        fake = root / "fake" / "bin" / "bash.exe"
        fake.parent.mkdir(parents=True)
        fake.write_bytes(b"MZfake-not-a-trusted-application")
        (fake.parent.parent / "git-bash.exe").write_bytes(fake.read_bytes())
        engine = fake.parent.parent / "usr" / "bin" / "bash.exe"
        engine.parent.mkdir(parents=True)
        engine.write_bytes(fake.read_bytes())
        os.environ["GITGO_BASH_PATH"] = str(fake)
        rejection = _resolve_bash()
        assert rejection.get("error") == "BASH_IDENTITY_UNVERIFIED", rejection
        results["fake_bash"] = {"rejected": True, "code": rejection["error"], "detail": rejection.get("detail")}
        (root / "result.json").write_text(json.dumps(results, indent=2))
        print(json.dumps({"run": str(root), "results": results}))
    finally:
        for key, value in saved.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value


if __name__ == "__main__":
    main()
