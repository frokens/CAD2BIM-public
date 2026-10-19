from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from CAD2BIM_backend.api import create_task_api
from CAD2BIM_backend.domain.control_plane import ArtifactRecord, ConflictError, ResourceNotFoundError, RunRecord, RunStatus, TaskRecord, TaskStatus
from CAD2BIM_backend.domain.identity import AuthenticatedSubject, TenantActorKind, TenantExecutionContext
from CAD2BIM_backend.ports.identity import IdentityAccessDenied


def _api(*, projection=None, artifacts=None, feedback=None):
    now = datetime.now(timezone.utc)
    subject = AuthenticatedSubject(
        issuer="https://identity.example.test", subject="reader", audience="cad2bim",
        authenticated_at=now, token_expires_at=now + timedelta(minutes=5),
    )
    identity = TenantExecutionContext(
        tenant_id="tenant-reader", principal_id="reader", task_id="task-one", actor_kind=TenantActorKind.USER,
        command_id="unit-read",
    )
    task = TaskRecord(
        task_id="task-one", owner_id=identity.tenant_id, title="图片任务", status=TaskStatus.RUNNING,
        active_module="component", metadata={"task_type": "component"}, created_at=now, updated_at=now,
    )
    run = RunRecord(
        run_id="run-one", task_id=task.task_id, owner_id=identity.tenant_id,
        module="component", stage_key="component", idempotency_key="first-request", thread_id="thread-one",
        created_at=now, updated_at=now,
    )
    service = SimpleNamespace(
        authorized_task_context=AsyncMock(return_value=(identity, task)),
        authorized_run_context=AsyncMock(return_value=(identity, run)),
        list_tasks=AsyncMock(return_value=((task, run),)),
        delete_task=AsyncMock(),
        pause_run=AsyncMock(return_value=run), continue_run=AsyncMock(return_value=run),
    )
    app = create_task_api(
        authenticator=SimpleNamespace(authenticate=lambda _token: subject), service=service,
        artifact_service=artifacts, bind_identity=lambda _context: nullcontext(), chat_projection=projection,
        feedback_service=feedback,
    )
    return app, service, task, run


@pytest.mark.asyncio
async def test_task_delete_requires_authentication_and_returns_empty_204():
    app, service, task, _ = _api()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test") as client:
        assert (await client.delete(f"/tasks/{task.task_id}")).status_code == 401
        service.delete_task.assert_not_awaited()
        response = await client.delete(f"/tasks/{task.task_id}", headers={"authorization": "Bearer unit"})
    assert response.status_code == 204 and response.content == b""
    assert service.delete_task.await_args.args[1] == task.task_id


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [
    (IdentityAccessDenied("private identity detail"), 403),
    (ResourceNotFoundError("private task detail"), 404),
    (ConflictError("Pause the running task and wait for it to stop before deleting it."), 409),
])
async def test_task_delete_projects_existing_authorization_and_execution_conflicts(error, expected):
    app, service, task, _ = _api()
    service.delete_task.side_effect = error
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
        headers={"authorization": "Bearer unit"}) as client:
        response = await client.delete(f"/tasks/{task.task_id}")
    assert response.status_code == expected
    if expected == 409:
        assert response.json()["detail"] == str(error)
    else:
        assert "private" not in response.text


async def test_resume_projects_paused_model_error_then_clears_it_on_the_original_run():
    app, service, task, run = _api()
    paused = run.model_copy(update={"status": RunStatus.PAUSED, "error": "model_network_interrupted"})
    service.pause_run.return_value = paused
    service.continue_run.return_value = run.model_copy(update={"status": RunStatus.QUEUED, "error": None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test",
        headers={"authorization": "Bearer unit"}) as client:
        status = await client.post(f"/tasks/{task.task_id}/runs/{run.run_id}/pause", json={"client_request_id": "pause-original"})
        assert status.json()["error"] == "model_network_interrupted"
        response = await client.post(f"/tasks/{task.task_id}/runs/{run.run_id}/resume", json={"client_request_id": "resume-original"})
    assert response.status_code == 200
    assert response.json()["run_id"] == run.run_id
    assert response.json()["status"] == "queued" and response.json()["error"] is None


def _artifact(*, task_id="task-one"):
    return ArtifactRecord(
        artifact_id="artifact-one", version_id="version-one", owner_id="tenant-reader", task_id=task_id,
        kind="input", filename="图片.png", content_type="image/png", size_bytes=4, sha256="a" * 64,
        idempotency_key="upload-one", created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_attachment_route_keeps_original_upload_identity_and_exact_task_scope():
    uploads = []
    async def upload(**kwargs):
        content = b"".join([chunk async for chunk in kwargs["chunks"]])
        uploads.append((kwargs["owner_id"], kwargs["task_id"], kwargs["idempotency_key"], kwargs["content_type"], content))
        return _artifact()
    artifacts = SimpleNamespace(upload=upload, download_version=AsyncMock(return_value=SimpleNamespace(artifact=_artifact(), content=b"png!")))
    app, _, _, _ = _api(artifacts=artifacts)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test", headers={"authorization": "Bearer unit"}) as client:
        for _ in range(2):
            response = await client.post("/tasks/task-one/attachments", data={"client_request_id": "upload-one"}, files={"file": ("图片.png", b"png!", "application/octet-stream")})
            assert response.status_code == 201
            assert response.json()["version_id"] == "version-one"
            assert "sha256" not in response.json()
        response = await client.get(response.json()["content_url"])
        assert response.content == b"png!"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"].startswith("inline; filename*=UTF-8''")
        artifacts.download_version.assert_awaited_once_with("tenant-reader", "artifact-one", "version-one")
        artifacts.download_version.return_value.artifact = _artifact(task_id="other-task")
        denied = await client.get("/tasks/task-one/artifacts/artifact-one/versions/version-one/content")
        assert denied.status_code == 404
        assert denied.content != b"png!"
    assert uploads == [("tenant-reader", "task-one", "upload-one", "image/png", b"png!")] * 2


@pytest.mark.asyncio
async def test_sse_authorizes_before_headers_and_keeps_durable_resume_cursor():
    streams = []
    async def stream(_subject, task_id, run_id, *, after_sequence):
        streams.append((task_id, run_id, after_sequence))
        yield "id: 9\nevent: item\ndata: {}\n\n"
        yield 'event: text_delta\ndata: {"text":"实时"}\n\n'
    projection = SimpleNamespace(stream=stream)
    app, service, _, _ = _api(projection=projection)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test", headers={"authorization": "Bearer unit"}) as client:
        response = await client.get("/tasks/task-one/runs/run-one/events?after_sequence=4", headers={"last-event-id": "7"})
        assert response.status_code == 200
        assert response.text.count("id:") == 1
        assert streams == [("task-one", "run-one", 7)]
        service.authorized_run_context.side_effect = ResourceNotFoundError("private run")
        response = await client.get("/tasks/task-one/runs/another-run/events")
        assert response.status_code == 404
        assert len(streams) == 1
        response = await client.get("/tasks/task-one/runs/run-one/events", headers={"last-event-id": "-2"})
        assert response.status_code == 422
        response = await client.get("/tasks/task-one/runs/run-one/events", headers={"last-event-id": "invalid"})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_unauthenticated_reads_never_reach_projection_or_artifact_core():
    projection = SimpleNamespace(item=AsyncMock(), snapshot=AsyncMock())
    artifacts = SimpleNamespace(download_version=AsyncMock())
    app, service, _, _ = _api(projection=projection, artifacts=artifacts)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test") as client:
        for path in ("/tasks/task-one/runs/run-one/snapshot", "/tasks/task-one/runs/run-one/items/item-one", "/tasks/task-one/artifacts/artifact-one/versions/version-one/content"):
            assert (await client.get(path)).status_code == 401
    service.authorized_run_context.assert_not_awaited()
    service.authorized_task_context.assert_not_awaited()
    artifacts.download_version.assert_not_awaited()
    projection.item.assert_not_awaited()
    projection.snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_list_and_controls_project_only_original_service_records():
    app, service, task, run = _api()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test", headers={"authorization": "Bearer unit"}) as client:
        page = (await client.get("/tasks?limit=1&after_task_id=task-zero")).json()
        assert page["tasks"][0]["task_type"] == "component"
        assert page["tasks"][0]["latest_run"]["run_id"] == run.run_id
        assert "owner_id" not in page["tasks"][0]
        assert "thread_id" not in page["tasks"][0]["latest_run"]
        service.list_tasks.assert_awaited_once_with(service.list_tasks.call_args.args[0], after_task_id="task-zero", limit=2)
        for action, command in (("pause", service.pause_run), ("resume", service.continue_run)):
            response = await client.post(f"/tasks/{task.task_id}/runs/{run.run_id}/{action}", json={"client_request_id": f"control-{action}"})
            assert response.status_code == 200
            assert response.json()["run_id"] == run.run_id
            assert command.call_args.args[1:] == (task.task_id, run.run_id, f"control-{action}")


@pytest.mark.asyncio
async def test_gate_passes_current_multimodal_content_and_rejects_retired_body():
    feedback = SimpleNamespace(respond_gate=AsyncMock(return_value=SimpleNamespace(
        interrupt_id="gate-one", status="responded", action="revise", responded_at=datetime.now(timezone.utc),
    )))
    app, _, _, _ = _api(feedback=feedback)
    path = "/tasks/task-one/runs/run-one/gates/gate-one/respond"
    content = [{"type": "text", "text": "请按图片调整"}, {"type": "image", "artifact_id": "artifact-one", "version_id": "version-one"}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://demo.test", headers={"authorization": "Bearer unit"}) as client:
        response = await client.post(path, json={"action": "revise", "content": content, "idempotency_key": "revise-one"})
        assert response.status_code == 200
        command = feedback.respond_gate.call_args.args[3]
        assert [block.model_dump() for block in command.content] == content
        response = await client.post(path, json={"action": "revise", "feedback_body": "old body", "idempotency_key": "old-one"})
        assert response.status_code == 422
        assert feedback.respond_gate.await_count == 1
