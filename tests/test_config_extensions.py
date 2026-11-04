"""Known owners can clear values without erasing unconsumed configuration."""
import json
from backend.core.config import ConfigManager
from backend.core.config_extensions import preserve_fields


def test_explicit_owned_maps_clear_but_unknown_fields_and_other_records_survive(tmp_path_factory):
    path = tmp_path_factory / "config.json"
    path.write_text(json.dumps({"projects":[{"name":"p","unavailable_entry":{
        "workspace":"missing","first_failed_at":"2026-11-01T00:00:00+00:00"},"future_project_field":7}],
        "keyboard_bindings":{"version":1,"overrides":{"execution.interrupt":{"key":"x","ctrl":True}}},
        "future_config_field":{"enabled":True}}),encoding="utf8")
    config = ConfigManager.load(path)
    config.projects[0].unavailable_entry = {}
    config.keyboard_bindings = {"version":1,"overrides":{}}
    ConfigManager.save(config,path)
    saved = json.loads(path.read_text(encoding="utf8"))
    assert saved["projects"][0]["unavailable_entry"] == {}
    assert saved["keyboard_bindings"]["overrides"] == {}
    assert saved["projects"][0]["future_project_field"] == 7
    assert saved["future_config_field"] == {"enabled":True}


def test_unowned_empty_mapping_does_not_claim_to_delete_future_fields():
    assert preserve_fields({"future":{"field":1}}, {"future":{}}) == {"future":{"field":1}}
