from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from CAD2BIM_backend.application.state_resource_service import (
    StateResourceBindingError,
    StateResourceNotFound,
    StateResourceService,
)
from CAD2BIM_backend.domain.checkpoint_cursor import CheckpointCursor
from CAD2BIM_backend.domain.safe_point_publications import checkpoint_cursor_digest
from CAD2BIM_backend.domain.state_read_binding import (
    STATE_RESOURCE_FIELDS,
    ExactStateReadBinding,
    StateFieldSource,
)
from CAD2BIM_backend.ports.graph_runtime import GraphCheckpointDescriptor, GraphCheckpointState


def _binding(*, child: bool = False, parent_fields: tuple[str, ...] = ()) -> ExactStateReadBinding:
    namespace = "role:requirement" if child else ""
    checkpoint_id = "child-one" if child else "root-one"
    checkpoint_map = (("", "root-one"),)
    if child:
        checkpoint_map += ((namespace, checkpoint_id),)
    return ExactStateReadBinding(
        owner_id="tenant-one",
        task_id="task-one",
        run_id="run-one",
        thread_id="thread-one",
        checkpoint_cursor=CheckpointCursor(
            thread_id="thread-one",
            checkpoint_id=checkpoint_id,
            checkpoint_ns=namespace,
            root_checkpoint_id="root-one",
            checkpoint_map=checkpoint_map,
            checkpoint_digest=checkpoint_cursor_digest(
                checkpoint_id=checkpoint_id,
                checkpoint_ns=namespace,
                checkpoint_map=checkpoint_map,
            ),
        ),
        field_sources=tuple(
            StateFieldSource(
                field=field,
                checkpoint_ns="" if not child or field in parent_fields else namespace,
                checkpoint_id="root-one" if not child or field in parent_fields else checkpoint_id,
            )
            for field in STATE_RESOURCE_FIELDS
        ),
    )


def _saved(binding: ExactStateReadBinding, values: dict, **changes) -> GraphCheckpointState:
    cursor = binding.checkpoint_cursor
    descriptor = GraphCheckpointDescriptor(
        thread_id=binding.thread_id,
        checkpoint_id=cursor.checkpoint_id,
        checkpoint_ns=cursor.checkpoint_ns,
        root_checkpoint_id=cursor.root_checkpoint_id,
        checkpoint_map=cursor.checkpoint_map,
        checkpoint_map_digest=cursor.checkpoint_digest,
    )
    return GraphCheckpointState(
        values=values,
        checkpoint=GraphCheckpointDescriptor.model_validate({**descriptor.model_dump(), **changes}),
        target_run_id=binding.run_id,
    )


class _Graph:
    def __init__(self, states: dict[tuple[str, str, str], GraphCheckpointState]) -> None:
        self.states = states
        self.calls: list[tuple[str, str, str]] = []

    async def get_checkpoint_state(self, *, thread_id, checkpoint_id, checkpoint_ns=""):
        key = (thread_id, checkpoint_id, checkpoint_ns)
        self.calls.append(key)
        return self.states.get(key)


@pytest.mark.asyncio
async def test_state_resources_preserve_none_empty_list_and_empty_markdown() -> None:
    root = _binding()
    root_key = ("thread-one", "root-one", "")
    graph = _Graph(
        {
            root_key: _saved(
                root,
                {
                    "requirements": None,
                    "clarifications": [],
                    "requirement_md": "",
                },
            )
        }
    )
    service = StateResourceService(graph)

    assert [item.uri for item in await service.list_resources(root)] == [
        "state://clarifications",
        "state://requirement-md",
    ]
    assert (await service.read_resource(root, "state://clarifications")).text == "[]"
    assert (await service.read_resource(root, "state://requirement-md")).text == ""
    with pytest.raises(StateResourceNotFound, match="absent"):
        await service.read_resource(root, "state://requirements")


@pytest.mark.asyncio
async def test_child_reads_only_its_exact_saved_values_without_parent_fallback() -> None:
    root = _binding()
    child = _binding(child=True)
    graph = _Graph(
        {
            ("thread-one", "root-one", ""): _saved(
                root,
                {
                    "requirements": {"sources": ["parent"], "requirements": []},
                    "clarifications": [
                        {
                            "id": "clar-one",
                            "topic": "x",
                            "question": "?",
                            "defaultAnswer": "y",
                            "notes": "",
                        }
                    ],
                    "requirement_md": "parent text",
                },
            ),
            ("thread-one", "child-one", "role:requirement"): _saved(
                child,
                {
                    "requirements": {
                        "sources": ["child"],
                        "requirements": [
                            {
                                "id": "req-one",
                                "purpose": "pipe",
                                "ifc_structure": "IfcPipeSegment",
                                "modeling_method": "axis",
                                "tools": ["CAD"],
                            }
                        ],
                    },
                    "clarifications": None,
                    "requirement_md": None,
                },
            ),
        }
    )
    service = StateResourceService(graph)

    assert [entry.uri for entry in await service.list_resources(child)] == ["state://requirements"]
    body = json.loads((await service.read_resource(child, "state://requirements")).text)
    assert body["sources"] == ["child"]
    assert body["requirements"][0]["id"] == "req-one"
    with pytest.raises(StateResourceNotFound, match="absent"):
        await service.read_resource(child, "state://requirement-md")
    assert graph.calls and all(
        call == ("thread-one", "child-one", "role:requirement") for call in graph.calls
    )


@pytest.mark.asyncio
async def test_partial_child_ownership_reads_parent_markdown_but_not_child_none() -> None:
    root = _binding()
    child = _binding(child=True, parent_fields=("requirement_md",))
    graph = _Graph(
        {
            ("thread-one", "root-one", ""): _saved(
                root,
                {
                    "requirements": None,
                    "clarifications": [{"id": "parent"}],
                    "requirement_md": "parent MD",
                },
            ),
            ("thread-one", "child-one", "role:requirement"): _saved(
                child,
                {"requirements": {"sources": [], "requirements": []}, "clarifications": None},
            ),
        }
    )
    service = StateResourceService(graph)

    assert [entry.uri for entry in await service.list_resources(child)] == [
        "state://requirements",
        "state://requirement-md",
    ]
    assert (await service.read_resource(child, "state://requirement-md")).text == "parent MD"
    with pytest.raises(StateResourceNotFound, match="absent"):
        await service.read_resource(child, "state://clarifications")
    assert ("thread-one", "root-one", "") in graph.calls
    assert ("thread-one", "child-one", "role:requirement") in graph.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    [
        {"thread_id": "another-thread"},
        {
            "checkpoint_id": "another-root", "root_checkpoint_id": "another-root",
            "checkpoint_map": (("", "another-root"),),
            "checkpoint_map_digest": checkpoint_cursor_digest(
                checkpoint_id="another-root", checkpoint_ns="",
                checkpoint_map=(("", "another-root"),),
            ),
        },
        {
            "checkpoint_map": (("", "root-one"), ("another-role", "another-child")),
            "checkpoint_map_digest": checkpoint_cursor_digest(
                checkpoint_id="root-one", checkpoint_ns="",
                checkpoint_map=(("", "root-one"), ("another-role", "another-child")),
            ),
        },
    ],
)
async def test_resource_read_rejects_exact_checkpoint_mismatch(mismatch: dict) -> None:
    binding = _binding()
    graph = _Graph(
        {("thread-one", "root-one", ""): _saved(binding, {"requirement_md": "saved"}, **mismatch)}
    )
    service = StateResourceService(graph)

    with pytest.raises(StateResourceBindingError, match="differs"):
        await service.list_resources(binding)


@pytest.mark.asyncio
async def test_resource_read_rejects_checkpoint_from_another_run() -> None:
    binding = _binding()
    saved = _saved(binding, {"requirement_md": "saved"})
    graph = _Graph({("thread-one", "root-one", ""): saved.model_copy(
        update={"target_run_id": "another-run"}
    )})
    with pytest.raises(StateResourceBindingError, match="differs"):
        await StateResourceService(graph).read_resource(binding, "state://requirement-md")


def test_read_binding_rejects_internal_thread_and_source_mismatch() -> None:
    raw = _binding().model_dump()
    with pytest.raises(ValidationError, match="thread"):
        ExactStateReadBinding.model_validate({**raw, "thread_id": "another-thread"})
    raw["field_sources"][0]["checkpoint_id"] = "another-checkpoint"
    with pytest.raises(ValidationError, match="source"):
        ExactStateReadBinding.model_validate(raw)
