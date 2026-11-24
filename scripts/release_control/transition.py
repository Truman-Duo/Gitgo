"""Bind native upgrade/rollback test results to the exact archives being released."""
from hashlib import sha256
import json
from pathlib import Path
from xml.etree import ElementTree

from .evidence import file_fact


WINDOWS = ("tests/test_update_windows_frozen.py", (
    "test_real_windows_upgrade_rollback_keep_program_and_user_data",
    "test_real_windows_bootstrap_automatically_applies_prepared_update_on_clean_exit",
    "test_broken_real_windows_dashboard_restores_bootable_predecessor",
    "test_frozen_windows_bootstrap_recovers_after_switch_interruption",
))
LINUX = ("tests/test_update_two_frozen_versions.py", (
    "test_actual_upgrade_and_explicit_rollback_keep_real_program_bootable",
    "test_actual_bad_frontend_restores_the_actual_predecessor",
    "test_actual_frozen_bootstrap_recovers_an_interrupted_switch",
    "test_actual_dashboard_confirmation_exits_and_bootstrap_applies_then_rolls_back",
))
MACOS = ("tests/test_update_macos_frozen.py", (
    "test_actual_quiescent_mac_upgrade_and_retained_rollback",
    "test_signed_but_broken_mac_frontend_restores_real_old_program",
    "test_actual_mac_bootstrap_recovers_an_interrupted_engine",
    "test_actual_mac_automatic_update_on_clean_exit",
))
SUITES = {"windows-x86_64": WINDOWS, "linux-x86_64": LINUX, "linux-aarch64": LINUX, "macos-arm64": MACOS}


def passing_cases(path, target):
    suite, expected = SUITES[target]
    fact = file_fact(path)
    if fact["bytes"] > 1024 * 1024:
        raise ValueError("Release transition test report exceeds its size limit")
    expected_class = suite.removesuffix(".py").replace("/", ".")
    found = set()
    raw = path.read_bytes()
    if len(raw) != fact["bytes"] or sha256(raw).hexdigest() != fact["sha256"]:
        raise ValueError("Release transition test report changed during verification")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid release transition test report") from exc
    for case in root.iter("testcase"):
        name = case.get("name", "").partition("[")[0]
        if case.get("classname") != expected_class or name not in expected:
            raise ValueError("Unexpected test in the release transition report")
        if any(case.find(state) is not None for state in ("failure", "error", "skipped")):
            raise ValueError("Required release transition test did not pass: " + name)
        found.add(name)
    if found != set(expected):
        raise ValueError("Release transition report does not cover all required cases")
    return fact


def verify_transition(plan, prepared, evidence):
    directory = plan.get("transition_evidence")
    if directory is None:
        raise ValueError("Native upgrade and rollback evidence is required to advance the update channel")
    directory = Path(directory)
    release = plan["release"]
    verified = {}
    for name, target in sorted(plan["targets"].items()):
        path = directory / (name + ".json")
        if file_fact(path)["bytes"] > 65536:
            raise ValueError("Release transition receipt exceeds its size limit")
        result = json.loads(path.read_bytes())
        old = json.loads(Path(target["baseline_manifest"]).read_bytes())
        previous = {key: old[key] for key in ("version", "source", "sha256")}
        candidate = {
            "version": release["version"],
            "source": {key: release[key] for key in ("commit", "tree")},
            "sha256": prepared["inputs"][target["archive"]]["sha256"],
        }
        if (type(result) is not dict or type(result.get("schema_version")) is not int or result["schema_version"] != 1
                or result.get("target") != name or result.get("previous") != previous
                or result.get("candidate") != candidate):
            raise ValueError("Release transition evidence belongs to a different archive pair: " + name)
        test_fact = passing_cases(directory / (name + ".xml"), name)
        if result.get("tests") != test_fact:
            raise ValueError("Release transition test report changed: " + name)
        verified[name] = {"previous": previous, "candidate": candidate, "tests": test_fact}
    evidence.record("release-transition", "success", targets=verified)
