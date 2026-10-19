"""Official package parsing/reads and final Provider multimodal projection."""

from pathlib import Path

import pytest
from PIL import Image
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.messages import messages_from_dict, messages_to_dict

from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
from CAD2BIM_backend.infrastructure.chatgpt_model import create_chatgpt_plan_model


@pytest.mark.asyncio
async def test_deployed_component_package_discovery_and_on_demand_reads():
    root = Path(__file__).resolve().parents[3] / "skills"
    package = root / "cad2bim-component-authoring"
    entry_body = (package / "SKILL.md").read_text().split("---", 2)[2].strip()
    page_path = package / "references" / "resource-use.md"
    page_body = page_path.read_text()
    skills = AgentSkills(root)
    await skills.discover()
    projected = skills.project_system_message(SystemMessage(content="Role instructions."))
    catalog = str(projected.content)
    maintenance = root / "cad2bim-installation-maintenance"
    maintenance_entry = (maintenance / "SKILL.md").read_text().split("---", 2)[2].strip()
    maintenance_page = (maintenance / "references/installation-management.md").read_text()
    assert "/cad2bim-component-authoring/SKILL.md" in catalog
    assert "/cad2bim-installation-maintenance/SKILL.md" in catalog
    assert "/component-engineering/SKILL.md" not in catalog
    assert entry_body.splitlines()[0] not in catalog
    assert page_body.splitlines()[0] not in catalog
    assert maintenance_entry.splitlines()[0] not in catalog
    assert maintenance_page.splitlines()[0] not in catalog

    for index, (path, body) in enumerate((
        ("SKILL.md", entry_body),
        ("references/resource-use.md", page_body),
    )):
        call = {
            "name": "read_file", "id": f"deployed-read-{index}",
            "args": {"file_path": f"/cad2bim-component-authoring/{path}"},
            "type": "tool_call",
        }
        result = await skills.aread(call, state={}, config={}, runtime=None)
        assert result.status == "success" and result.tool_call_id == call["id"]
        assert body.splitlines()[0] in str(result.content)

    for path, body in (("SKILL.md", maintenance_entry),
                       ("references/installation-management.md", maintenance_page)):
        result = await skills.aread({"id": "maintenance-read",
            "args": {"file_path": f"/cad2bim-installation-maintenance/{path}"}},
            state={}, config={}, runtime=None)
        assert result.status == "success" and body.splitlines()[0] in str(result.content)

    removed = await skills.aread({"id": "old-consumer-page",
        "args": {"file_path": "/cad2bim-component-authoring/references/product-apply.md"}},
        state={}, config={}, runtime=None)
    assert removed.status == "error"


@pytest.mark.asyncio
async def test_standard_package_sidefiles_images_saved_messages_and_provider_request(tmp_path):
    package = tmp_path / "method"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: method\ndescription: Read geometry references.\n---\nRead supporting files.\n")
    (package / "example.py").write_text("raise AssertionError('must never execute')\n")
    (package / "params.json").write_text('{"unit": "mm"}\n')
    Image.new("RGB", (8, 8), "red").save(package / "view.png")
    skills = AgentSkills(tmp_path)
    await skills.discover()
    projected = skills.project_system_message(SystemMessage(content="Role instructions."))
    assert "/method/SKILL.md" in str(projected.content)
    results = []
    calls = []
    for index, path in enumerate(("SKILL.md", "example.py", "params.json", "view.png")):
        call = {"name": "read_file", "id": f"read-{index}", "args": {"file_path": f"/method/{path}"}, "type": "tool_call"}
        calls.append(call)
        result = await skills.aread(call, state={}, config={}, runtime=None)
        assert result.status == "success" and result.tool_call_id == call["id"]
        results.append(result)
    assert "must never execute" in results[1].content
    assert '"unit": "mm"' in results[2].content
    assert isinstance(results[3].content, list)
    assert results[3].content[0]["type"] == "image"
    saved = messages_from_dict(messages_to_dict([AIMessage(content="", tool_calls=calls), *results]))
    # A new startup discovers the current package, while saved reads retain the
    # exact prior body in canonical messages. There is no snapshot side store.
    (package / "params.json").write_text('{"unit": "m"}\n')
    restarted = AgentSkills(tmp_path)
    await restarted.discover()
    reread = await restarted.aread(calls[2], state={}, config={}, runtime=None)
    assert '"unit": "m"' in reread.content and '"unit": "mm"' in saved[3].content
    model = create_chatgpt_plan_model(lambda: "no-network-unit-test")
    definitions = model.bind_tools([skills.read_tool]).kwargs["tools"]
    payload = model._get_request_payload([projected, HumanMessage(content="Read the skill."), *saved], tools=definitions)
    import json
    encoded = json.dumps(payload)
    assert "input_image" in encoded and "data:image/png;base64," in encoded
    assert "read_file" in encoded and "skill_load" not in encoded
    assert "skills_metadata" not in encoded and "tool_token_limit_before_evict" not in encoded


@pytest.mark.asyncio
async def test_skill_backend_cannot_read_parent_host_or_external_symlink(tmp_path):
    root = tmp_path / "packages"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("outside package")
    (root / "escape.txt").symlink_to(tmp_path / "outside.txt")
    skills = AgentSkills(root)
    await skills.discover()
    for path in ("/../outside.txt", str(tmp_path / "outside.txt"), "/escape.txt"):
        result = await skills.aread({"id": "denied", "args": {"file_path": path}}, state={}, config={}, runtime=None)
        assert result.status == "error" and "outside package" not in str(result.content)
