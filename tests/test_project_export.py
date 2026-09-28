import json
from types import SimpleNamespace

import pytest

from backend.core.application.services import ApplicationServices, OperationError


def _service(monkeypatch, workspace):
    service = ApplicationServices()
    monkeypatch.setattr(
        service, "_sync_session",
        lambda project, scan, trial: SimpleNamespace(workspace_path=str(workspace)),
    )
    import backend.core.governance as governance
    monkeypatch.setattr(
        governance, "collect_state_bundle",
        lambda session, minimal, include_identity: {
            "project": "demo", "minimal": minimal,
            "identity": {"included": include_identity},
        },
    )
    return service


@pytest.mark.parametrize("format_name,suffix", [
    ("json", ".json"), ("yaml", ".yaml"), ("markdown", ".md"),
])
def test_project_export_writes_selected_format_atomically(
    monkeypatch, tmp_path_factory, format_name, suffix,
):
    service = _service(monkeypatch, tmp_path_factory)

    result = service.project_export(
        "demo", minimal=True, output_path="exports/state",
        output_format=format_name,
    )

    target = tmp_path_factory / "exports" / f"state{suffix}"
    assert result["path"] == str(target)
    assert result["format"] == format_name
    assert target.is_file()
    assert result["size"] == target.stat().st_size
    if format_name == "json":
        assert json.loads(target.read_text(encoding="utf-8"))["project"] == "demo"


def test_project_export_refuses_silent_overwrite(monkeypatch, tmp_path_factory):
    service = _service(monkeypatch, tmp_path_factory)
    target = tmp_path_factory / "state.json"
    target.write_text("keep", encoding="utf-8")

    with pytest.raises(OperationError) as raised:
        service.project_export("demo", output_path=str(target), output_format="json")

    assert raised.value.code == "EXPORT_DESTINATION_EXISTS"
    assert target.read_text(encoding="utf-8") == "keep"
