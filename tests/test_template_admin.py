import json

import pytest

from comfyui_runpod_agentic import template_admin
from comfyui_runpod_agentic.template_admin import load_id_map, redact_template_payload, save_templates, template_key


class FakeTemplateClient:
    def __init__(self):
        self.inputs = []

    def save_template(self, input):
        self.inputs.append(input)
        return {"id": f"id-{len(self.inputs)}", "name": input["name"]}


def test_save_templates_persists_ids_and_reuses_existing_id(tmp_path):
    spec = tmp_path / "templates.json"
    template_map = tmp_path / "ids.json"
    spec.write_text(json.dumps({"templates": [{"key": "rp-agent-pi", "name": "crag-agent-pi", "imageName": "ubuntu:24.04", "containerDiskInGb": 5, "volumeInGb": 0}]}))
    client = FakeTemplateClient()

    first = save_templates(spec, template_map, client=client)
    second = save_templates(spec, template_map, client=client)

    assert first == {"rp-agent-pi": "id-1"}
    assert second == {"rp-agent-pi": "id-2"}
    assert client.inputs[1]["id"] == "id-1"
    assert json.loads(template_map.read_text()) == {"rp-agent-pi": "id-2"}


def test_save_templates_dry_run_redacts_secret_env(tmp_path, capsys):
    spec = tmp_path / "templates.json"
    template_map = tmp_path / "ids.json"
    spec.write_text(
        json.dumps(
            [
                {
                    "key": "rp-secret",
                    "name": "crag-secret",
                    "imageName": "ubuntu:24.04",
                    "env": [{"key": "API_TOKEN", "value": "secret"}, {"key": "MODE", "value": "test"}],
                }
            ]
        )
    )

    result = save_templates(spec, template_map, client=FakeTemplateClient(), dry_run=True)
    output = capsys.readouterr().out

    assert result == {}
    assert "<redacted>" in output
    assert '"value": "secret"' not in output
    assert not template_map.exists()


def test_template_admin_rejects_invalid_inputs(tmp_path):
    invalid_map = tmp_path / "ids.json"
    invalid_map.write_text("[]")
    invalid_spec = tmp_path / "templates.json"
    invalid_spec.write_text(json.dumps({"templates": {"bad": True}}))

    with pytest.raises(ValueError, match="key or name"):
        template_key({})
    with pytest.raises(ValueError, match="JSON object"):
        load_id_map(invalid_map)
    with pytest.raises(ValueError, match="templates list"):
        save_templates(invalid_spec, tmp_path / "out.json", client=FakeTemplateClient())


def test_template_admin_main_uses_cli_args(monkeypatch, tmp_path):
    spec = tmp_path / "templates.json"
    template_map = tmp_path / "ids.json"
    spec.write_text(json.dumps([{"key": "rp-agent", "name": "crag-agent", "imageName": "ubuntu:24.04"}]))
    calls = []

    def fake_save_templates(spec_path, map_path, *, dry_run=False):
        calls.append((spec_path, map_path, dry_run))
        return {}

    monkeypatch.setattr(template_admin, "save_templates", fake_save_templates)
    monkeypatch.setattr("sys.argv", ["create-runpod-templates", "--spec", str(spec), "--map", str(template_map), "--dry-run"])

    assert template_admin.main() == 0
    assert calls == [(spec, template_map, True)]


def test_redact_template_payload_ignores_non_secret_env():
    payload = redact_template_payload({"env": [{"key": "MODE", "value": "test"}]})

    assert payload["env"] == [{"key": "MODE", "value": "test"}]
