from comfyui_runpod_agentic.runpod_options import datacenters_from_gpu_types, optional_combo_or_string, sorted_unique_ids


def test_sorted_unique_ids_extracts_ids():
    assert sorted_unique_ids([{"id": "US-KS-2"}, {"id": ""}, {"id": "EU-RO-1"}, {"id": "US-KS-2"}]) == ["EU-RO-1", "US-KS-2"]


def test_optional_combo_or_string_falls_back_to_editable_string():
    assert optional_combo_or_string([]) == ("STRING", {"default": ""})


def test_optional_combo_or_string_keeps_blank_choice():
    assert optional_combo_or_string(["NVIDIA RTX A4000"]) == (["", "NVIDIA RTX A4000"],)


def test_datacenters_from_gpu_types_extracts_node_group_datacenters():
    gpu_types = [{"id": "gpu", "nodeGroupDatacenters": [{"id": "US-KS-2"}, {"id": "EU-RO-1"}]}]

    assert datacenters_from_gpu_types(gpu_types) == [{"id": "US-KS-2"}, {"id": "EU-RO-1"}]
