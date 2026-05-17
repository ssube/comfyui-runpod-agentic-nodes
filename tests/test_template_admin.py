import json

from comfyui_runpod_agentic.template_admin import save_templates


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
