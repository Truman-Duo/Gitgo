"""测试 Project Contract + Drift Detection"""

import tempfile
import shutil
from pathlib import Path

from backend.core.contract import (
    ProjectContract,
    DecidedFeature,
    ContractManager,
    detect_drift,
    _detect_new_imports,
)


def _tmp():
    d = tempfile.mkdtemp()
    return Path(d)


def _rm(p: Path):
    shutil.rmtree(str(p), ignore_errors=True)


# ── ProjectContract ─────────────────────────────────────

def test_contract_to_dict():
    c = ProjectContract(
        project="test",
        tech_stack=["python", "pyside6"],
        decided_features=[
            DecidedFeature(name="dark_mode", location="src/ui.py",
                           signature="def dark_mode", confirmed_count=2),
        ],
        architecture_constraints=["不使用绝对定位"],
    )
    d = c.to_dict()
    assert d["project"] == "test"
    assert d["tech_stack"] == ["python", "pyside6"]
    assert len(d["decided_features"]) == 1
    assert d["decided_features"][0]["confirmed_count"] == 2


def test_contract_from_dict():
    d = {
        "project": "test",
        "updated": "2026-05-19",
        "tech_stack": ["python"],
        "decided_features": [
            {"name": "feat_a", "location": "a.py", "signature": "def a", "confirmed_count": 1},
        ],
        "architecture_constraints": ["规则1"],
    }
    c = ProjectContract.from_dict(d)
    assert c.project == "test"
    assert len(c.decided_features) == 1
    assert c.decided_features[0].name == "feat_a"


def test_contract_from_empty_dict():
    c = ProjectContract.from_dict({})
    assert c.project == ""
    assert c.decided_features == []


# ── ContractManager ─────────────────────────────────────

def test_save_and_load():
    p = _tmp()
    try:
        (p / ".gitgo").mkdir()
        c = ProjectContract(project="proj", tech_stack=["python"])
        ContractManager.save(p, c)
        assert (p / ".gitgo" / "contract.yaml").exists()
        loaded = ContractManager.load(p)
        assert loaded is not None
        assert loaded.project == "proj"
        assert loaded.tech_stack == ["python"]
    finally:
        _rm(p)


def test_load_nonexistent():
    p = _tmp()
    try:
        assert ContractManager.load(p) is None
    finally:
        _rm(p)


def test_update_feature_new():
    p = _tmp()
    try:
        c = ContractManager.update_feature(p, "myproj", "feat_x", location="x.py")
        assert len(c.decided_features) == 1
        assert c.decided_features[0].name == "feat_x"
        assert c.decided_features[0].confirmed_count == 1
    finally:
        _rm(p)


def test_update_feature_existing_increments():
    p = _tmp()
    try:
        ContractManager.update_feature(p, "p", "feat_a", location="a.py")
        c = ContractManager.update_feature(p, "p", "feat_a", location="a2.py")
        assert c.decided_features[0].confirmed_count == 2
        assert c.decided_features[0].location == "a2.py"
        assert len(c.decided_features) == 1
    finally:
        _rm(p)


# ── Drift Detection ─────────────────────────────────────

def test_drift_feature_deleted():
    p = _tmp()
    try:
        c = ProjectContract(
            project="t",
            decided_features=[
                DecidedFeature(name="dark", location="ui.py", confirmed_count=3),
            ],
        )
        alerts = detect_drift(p, ["ui.py"], c)
        assert any(a["rule"] == "feature_deleted" for a in alerts)
    finally:
        _rm(p)


def test_drift_feature_sig_lost():
    p = _tmp()
    try:
        (p / "ui.py").write_text("def other(): pass")
        c = ProjectContract(
            project="t",
            decided_features=[
                DecidedFeature(name="dark", location="ui.py",
                               signature="def dark_mode", confirmed_count=2),
            ],
        )
        alerts = detect_drift(p, ["ui.py"], c)
        assert any(a["rule"] == "feature_signature_lost" for a in alerts)
    finally:
        _rm(p)


def test_drift_no_alerts_when_ok():
    p = _tmp()
    try:
        (p / "ui.py").write_text("def dark_mode(): pass\n")
        c = ProjectContract(
            project="t",
            decided_features=[
                DecidedFeature(name="dark", location="ui.py",
                               signature="def dark_mode", confirmed_count=2),
            ],
        )
        alerts = detect_drift(p, ["ui.py"], c)
        assert len(alerts) == 0
    finally:
        _rm(p)


def test_drift_tech_stack_new_import():
    p = _tmp()
    try:
        (p / "m.py").write_text("import tkinter\nfrom flask import Flask\n")
        c = ProjectContract(project="t", tech_stack=["pyside6"])
        new_imports = _detect_new_imports(p, ["m.py"], c)
        assert "tkinter" in new_imports
        assert "flask" in new_imports
    finally:
        _rm(p)


def test_drift_tech_stack_declared_ok():
    p = _tmp()
    try:
        (p / "m.py").write_text("from pyside6.QtWidgets import QWidget\n")
        c = ProjectContract(project="t", tech_stack=["pyside6"])
        new_imports = _detect_new_imports(p, ["m.py"], c)
        assert new_imports == []
    finally:
        _rm(p)


def test_drift_builtin_not_flagged():
    p = _tmp()
    try:
        (p / "m.py").write_text("import os\nimport json\nfrom pathlib import Path\n")
        c = ProjectContract(project="t", tech_stack=["pyside6"])
        new_imports = _detect_new_imports(p, ["m.py"], c)
        assert "os" not in new_imports
        assert "json" not in new_imports
    finally:
        _rm(p)


def test_detect_drift_empty_contract():
    alerts = detect_drift(Path("."), [], ProjectContract())
    assert alerts == []


def test_detect_drift_none_contract():
    assert detect_drift(Path("."), [], None) == []
