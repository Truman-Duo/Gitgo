"""Resumable local recipes with explicit file/dependency identities."""
import os
import platform
from pathlib import Path
import shutil
import subprocess
import time

from .evidence import digest, file_fact
from .plan import check_source
from .processes import owned
from backend.core.publish.release_dag import run_dag


def run_checks(plan, evidence, workers):
    def execute(step, dependencies):
        name = 'check-' + step['id']
        inputs = {p: file_fact(p) for p in step['inputs']}
        executable = step['argv'][0]
        if Path(executable).is_absolute() or '/' in executable or '\\' in executable:
            executable = str((Path(step['cwd']) / executable).resolve())
        else:
            executable = shutil.which(executable)
        if executable is None:
            raise ValueError('Release check executable is unavailable')
        binding = digest({'recipe': step, 'inputs': inputs,
                          'dependencies': dependencies,
                          'executable': file_fact(executable),
                          'environment': {k: digest(os.environ.get(k)) for k in
                              {'PATH', 'PYTHONPATH', 'VIRTUAL_ENV', *step.get('environment_inputs', [])}},
                          'platform': platform.platform(), 'python': platform.python_version()})
        previous = evidence.read(name)
        outputs = lambda: {p: file_fact(p) for p in step['outputs']}
        if previous and previous.get('state') == 'success' and previous.get('binding') == binding:
            try:
                if outputs() == previous['outputs']:
                    evidence.record('reuse-' + name, 'reused', binding=binding)
                    return previous
            except ValueError:
                pass
        started = time.monotonic()
        evidence.record(name, 'running', binding=binding)
        # Local-only logs never enter the uploaded asset list. Recipes must not
        # print credentials; the controller never serializes its environment.
        log = evidence.root / (name + '.log')
        try:
            with log.open('ab') as stream:
                with owned(step['argv'], cwd=step['cwd'], stdin=subprocess.DEVNULL,
                           single_process=step.get('process_scope') == 'single_process',
                           stdout=stream, stderr=subprocess.STDOUT) as result:
                    result.wait(timeout=step.get('timeout', 1800))
            if result.returncode:
                raise ValueError('Local release check failed: ' + step['id'])
            # A test/build is not allowed to silently change the candidate.
            check_source(plan)
            if {p: file_fact(p) for p in step['inputs']} != inputs:
                raise ValueError('Release input changed during check: ' + step['id'])
            return evidence.record(name, 'success', binding=binding, outputs=outputs(),
                                   seconds=time.monotonic() - started)
        except BaseException as exc:
            evidence.record(name, 'failed', binding=binding, error=type(exc).__name__,
                            seconds=time.monotonic() - started)
            raise
    return run_dag(plan['checks'], execute, workers=workers)
