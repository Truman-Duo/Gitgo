"""Actual runtime fingerprints, early entry and bounded native probe failures."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.update import DataCompatibility, UpdateError
from backend.core.update.runtime_contract import _canonical_code, code_evidence, data_probe, runtime_evidence
from backend.core.update.tree import encode
from backend.core.update.health import probe_host
from scripts import stage_update_contract


def test_actual_sql_fingerprints_and_writer_evidence_are_bound():
    from backend.core.storage.migrations import STATE_MIGRATIONS, OBSERVABILITY_MIGRATIONS
    compatibility, evidence = runtime_evidence()
    DataCompatibility.parse(compatibility)
    for name, migrations in (("state", STATE_MIGRATIONS), ("observability", OBSERVABILITY_MIGRATIONS)):
        assert compatibility[name + "_migrations"] == [
            {"version": item.version, "sha256": item.checksum} for item in migrations]
        for item in migrations:
            assert evidence[f"update-evidence/{name}-{item.version}.sql"] == item.sql.encode()
    for name in ("config", "provider"):
        assert compatibility[name + "_migration_sha256"] == hashlib.sha256(
            evidence["update-evidence/" + name + ".json"]).hexdigest()


def test_code_provenance_is_normalized_but_code_change_is_not():
    original = compile("def f():\n return 1\n", "source-location", "exec")
    frozen = compile("def f():\n return 1\n", "frozen-location", "exec")
    changed = compile("def f():\n return 2\n", "frozen-location", "exec")
    assert encode(_canonical_code(original, "writer")) == encode(_canonical_code(frozen, "writer"))
    assert encode(_canonical_code(original, "writer")) != encode(_canonical_code(changed, "writer"))


def test_uninspectable_writer_is_refused(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: SimpleNamespace(loader=object()))
    with pytest.raises(UpdateError, match="UPDATE_RUNTIME_CONTRACT"):
        code_evidence(("uninspectable",))


def test_early_probe_never_loads_user_state_or_native_secrets(monkeypatch, capsys):
    from backend.core.config import ConfigManager
    from backend.core.llm_config import LLMConfigManager
    from backend.core.native_host_entry import main
    from backend.core.secret_store import EncryptedSecretStore
    def forbidden(*args, **kwargs):
        pytest.fail("read-only data probe touched user configuration or secret store")
    monkeypatch.setattr(ConfigManager, "load", forbidden)
    monkeypatch.setattr(LLMConfigManager, "load", forbidden)
    monkeypatch.setattr(EncryptedSecretStore, "__init__", forbidden)
    assert main(["--gitgo-internal-role", "update-data-probe", "--nonce", "a" * 32]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["nonce"] == "a" * 32 and report["memory_sqlite_check"] is True
    assert report["data_compatibility"] == runtime_evidence()[0]


def test_extra_probe_arguments_do_not_reach_the_application():
    from backend.core.native_host_entry import main
    with pytest.raises(SystemExit):
        main(["--gitgo-internal-role", "update-data-probe", "--nonce", "a" * 32, "--project", "private"])


@pytest.mark.parametrize("command", ["ATTACH DATABASE '{target}' AS outside;", "VACUUM INTO '{target}';",
                                    "PRAGMA temp_store=FILE;", "SELECT load_extension('{target}');"])
def test_memory_validation_cannot_create_external_database(tmp_path_factory, monkeypatch, command):
    import sqlite3
    from backend.core.storage import runtime
    from backend.core.storage.migrations import Migration
    target = tmp_path_factory / "must-not-create.sqlite"
    monkeypatch.setattr(runtime, "STATE_MIGRATIONS", (Migration(1, "unsafe-test", command.format(target=target)),))
    with pytest.raises(sqlite3.DatabaseError):
        runtime.validate_in_memory_migrations()
    assert not target.exists()


def fake_stage(root):
    from scripts.render_product_manifest import render_manifest
    source = Path(__file__).resolve().parents[1] / "packaging/product.json"
    product = render_manifest(source, "macos", "arm64")
    (root / "product.json").write_text(json.dumps(product))
    return product


def fake_probe(product):
    return {"runtime": {**{key: product[key] for key in ("product_id", "version", "platform", "architecture")},
                        "data_compatibility": runtime_evidence()[0]},
            "cleanup": {"mode": "owned-session-only", "complete": False,
                        "issues": ["detached_descendants_not_covered"]}}


def test_stage_writes_matching_evidence_without_claiming_cleanup(tmp_path_factory, monkeypatch):
    root = tmp_path_factory
    product = fake_stage(root)
    monkeypatch.setattr(stage_update_contract, "probe_host", lambda *_: fake_probe(product))
    contract = stage_update_contract.stage_contract(root)
    assert contract["data_compatibility"] == runtime_evidence()[0]
    for item in contract["state_migrations"] + contract["observability_migrations"] + [contract["config"], contract["provider"]]:
        assert hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    report = json.loads((root / "update-probe-result.json").read_text())
    assert report["scope_cleanup"]["complete"] is False
    assert report["update_commit_authorized"] is False
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises((FileExistsError, ValueError)):
        stage_update_contract.stage_contract(root)
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("field", ["version", "platform", "architecture", "product_id", "data_compatibility"])
def test_frozen_mismatch_prevents_authoritative_contract(tmp_path_factory, monkeypatch, field):
    product = fake_stage(tmp_path_factory)
    response = fake_probe(product)
    response["runtime"][field] = {} if field == "data_compatibility" else "different"
    monkeypatch.setattr(stage_update_contract, "probe_host", lambda *_: response)
    with pytest.raises(ValueError):
        stage_update_contract.stage_contract(tmp_path_factory)
    assert not (tmp_path_factory / "update-data-contract.json").exists()
    assert not (tmp_path_factory / "update-evidence").exists()


def test_parent_does_not_forward_secrets_or_confuse_child_health_with_cleanup(tmp_path_factory, monkeypatch):
    from backend.core import process_control, process_output
    from backend.core.update import health
    root = tmp_path_factory.resolve()
    host = root / "host"
    host.write_bytes(b"mocked-executable-not-run")
    profile = root / "probe"
    profile.mkdir(mode=0o700)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-never-forwarded")
    monkeypatch.setenv("GITGO_CONFIG_PATH", "must-not-inherit")
    captured = {}
    cleanup = {"mode": "owned-session-only", "complete": False, "issues": ["detached_descendants_not_covered"]}
    proc = SimpleNamespace(returncode=0, _gitgo_cleanup=cleanup, poll=lambda: 0)
    def spawn(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return proc
    def output(*args, **kwargs):
        report = data_probe(captured["argv"][-1])
        return json.dumps(report), "", False
    monkeypatch.setattr(process_control, "spawn_managed", spawn)
    monkeypatch.setattr(process_control, "attach_kill_job", lambda _: None)
    monkeypatch.setattr(process_control, "close_job", lambda _: None)
    monkeypatch.setattr(process_output, "collect_output", output)
    result = probe_host(host, profile)
    assert "DEEPSEEK_API_KEY" not in captured["env"]
    assert captured["env"]["GITGO_CONFIG_PATH"] == str(profile / "config.json")
    assert captured["cwd"] == str(profile)
    if health.os.name == "posix":
        assert result["cleanup"] == cleanup  # False is retained, never promoted to True.


@pytest.mark.parametrize("failure", ["exit", "overflow", "timeout", "bad_nonce", "malformed", "missing_cleanup"])
def test_probe_failures_are_bounded_and_sanitized(tmp_path_factory, monkeypatch, failure):
    from backend.core import process_control, process_output
    import subprocess
    root = tmp_path_factory.resolve()
    host = root / "host"
    host.write_bytes(b"not-run")
    profile = root / "profile"
    profile.mkdir(mode=0o700)
    captured = {}
    proc = SimpleNamespace(returncode=1 if failure == "exit" else 0, poll=lambda: 0,
                           _gitgo_cleanup=None if failure == "missing_cleanup" else {"complete": True})
    def spawn(argv, **kwargs):
        captured["argv"] = argv
        return proc
    def output(*args, **kwargs):
        if failure == "timeout": raise subprocess.TimeoutExpired("private-path", 1)
        if failure == "overflow": raise process_output.OutputLimit("private-content")
        if failure == "malformed": return "not JSON", "private stderr", False
        report = data_probe("b" * 32 if failure == "bad_nonce" else captured["argv"][-1])
        return json.dumps(report), "private stderr", False
    monkeypatch.setattr(process_control, "spawn_managed", spawn)
    monkeypatch.setattr(process_control, "attach_kill_job", lambda _: None)
    monkeypatch.setattr(process_control, "close_job", lambda _: None)
    monkeypatch.setattr(process_output, "collect_output", output)
    if failure == "missing_cleanup" and __import__("os").name != "posix":
        pytest.skip("Windows adapter reports Job authority instead of POSIX cleanup")
    with pytest.raises(UpdateError, match="UPDATE_HEALTH_FAILED") as error:
        probe_host(host, profile)
    assert "private" not in str(error.value)


def test_evidence_write_failure_never_publishes_partial_contract(tmp_path_factory, monkeypatch):
    product = fake_stage(tmp_path_factory)
    monkeypatch.setattr(stage_update_contract, "probe_host", lambda *_: fake_probe(product))
    original = Path.open
    def fail(path, *args, **kwargs):
        if path.name == "config.json" and path.parent.name == "update-evidence":
            raise OSError("injected disk-full")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(OSError):
        stage_update_contract.stage_contract(tmp_path_factory)
    assert not (tmp_path_factory / "update-data-contract.json").exists()


@pytest.mark.parametrize("changed", ["none", "cleanup", "runtime", "compatibility"])
def test_transaction_health_requires_runtime_match_and_owned_cleanup(tmp_path_factory, monkeypatch, changed):
    from backend.core.update import health
    root = tmp_path_factory
    product = fake_stage(root)
    compatibility = runtime_evidence()[0]
    (root / "build-manifest.json").write_text(json.dumps({"source": {"commit": "a" * 40, "tree": "b" * 40}}))
    (root / "update-data-contract.json").write_text(json.dumps({"data_compatibility": compatibility}))
    result = fake_probe(product)
    result["cleanup"]["complete"] = changed != "cleanup"
    if changed == "runtime": result["runtime"]["version"] = "9.0.0"
    if changed == "compatibility": result["runtime"]["data_compatibility"]["config_schema_version"] += 1
    monkeypatch.setattr(health, "probe_host", lambda *_: result)
    if changed != "none":
        with pytest.raises(UpdateError, match="UPDATE_HEALTH_FAILED"):
            health.frozen_health_check(root, root)
    else:
        report = health.frozen_health_check(root, root)
        assert report.passed is True and report.source_commit == "a" * 40


def test_all_packagers_keep_update_evidence_opt_in_before_archive_inventory():
    root = Path(__file__).resolve().parents[1]
    for name in ("build_macos.sh", "build_linux.sh"):
        script = (root / "packaging" / name).read_text()
        assert "UPDATE_EVIDENCE=0" in script
        assert "--update-evidence) UPDATE_EVIDENCE=1" in script
        assert script.index("stage_update_contract.py") < script.index("unix_release_manifest.py")
    windows = (root / "packaging/build_windows.ps1").read_text()
    assert "[switch]$UpdateEvidence" in windows and "if ($UpdateEvidence)" in windows
    assert windows.index("stage_update_contract.py") < windows.index("--stage $stage --output")
