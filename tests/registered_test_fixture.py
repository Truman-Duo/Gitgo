"""Explicit developer pytest executor for manifest/version unit tests only."""
from functools import partial
from backend.core.loop.test_manifest import run_registered_test as _record_test
from backend.core.tools.registered_test import execute_test_seeds
run_registered_test = partial(_record_test, execute=execute_test_seeds)
