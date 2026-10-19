from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from CAD2BIM_backend.api import create_task_api
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.domain.control_plane import (
    FeedbackCommandReceipt,
    FeedbackCommandStatus,
)
from CAD2BIM_backend.domain.system_interrupts import (
    SystemInterruptKind,
    SystemInterruptResponseReceipt,
    SystemInterruptSource,
)
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.infrastructure.authentication import (
    AuthenticationError,
    JwtRs256Authenticator,
)
from CAD2BIM_backend.infrastructure.memory.control_plane_repository import (
    InMemoryControlPlaneRepository,
)
from CAD2BIM_backend.infrastructure.memory.identity_directory import InMemoryIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context


ISSUER = "https://identity.example.test"
AUDIENCE = "cad2bim-api"


def _keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public_pem


def _token(private, *, subject="alice", **changes):
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "sub": subject,
    }
    claims.update(changes)
    return jwt.encode(claims, private, algorithm="RS256")


def _binding(module: str, stage_key: str) -> ExecutionProtocolBinding:
    return ExecutionProtocolBinding(
        bundle_id="unit_bundle", module=module, stage_key=stage_key
    )


def test_rs256_authenticator_rejects_forged_claims_and_expiry() -> None:
    private, public_pem = _keys()
    authenticator = JwtRs256Authenticator(
        public_key_pem=public_pem, issuer=ISSUER, audience=AUDIENCE
    )
    subject = authenticator.authenticate(_token(private))
    assert (subject.issuer, subject.subject, subject.audience) == (ISSUER, "alice", AUDIENCE)
    assert "token" not in subject.model_dump()

    other_private, _ = _keys()
    invalid = [
        _token(other_private),
        _token(private, iss="https://other.example.test"),
        _token(private, aud="another-api"),
        _token(private, exp=datetime.now(timezone.utc) - timedelta(seconds=1)),
        _token(private, subject=" "),
        jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "alice"}, private, algorithm="RS256"),
        jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": 9999999999},
            "untrusted-secret-with-sufficient-test-length",
            algorithm="HS256",
        ),
    ]
    for credential in invalid:
        with pytest.raises(AuthenticationError, match="invalid bearer credential"):
            authenticator.authenticate(credential)


def test_http_task_run_entry_rejects_scope_fields_and_hides_lease() -> None:
    private, public_pem = _keys()
    repository = InMemoryControlPlaneRepository()
    service = TaskRunService(
        repository=repository,
        identity_directory=InMemoryIdentityDirectory(),
        execution_protocol_factory=_binding,
        bind_identity=bind_tenant_execution_context,
    )
    client = TestClient(
        create_task_api(
            authenticator=JwtRs256Authenticator(
                public_key_pem=public_pem, issuer=ISSUER, audience=AUDIENCE
            ),
            service=service,
        )
    )
    alice = {"Authorization": f"Bearer {_token(private, subject='alice')}"}
    bob = {"Authorization": f"Bearer {_token(private, subject='bob')}"}

    assert client.post("/tasks", json={"title": "x"}).status_code == 401
    assert (
        client.post("/tasks", headers=alice, json={"title": "x", "tenant_id": "forged"}).status_code
        == 422
    )
    created = client.post("/tasks", headers=alice, json={"title": "First"})
    assert created.status_code == 201
    task_id = created.json()["task_id"]
    assert "tenant_id" not in created.json()
    assert client.get(f"/tasks/{task_id}", headers=bob).status_code == 404
    invalid_path = client.get("/tasks/bad!", headers=alice)
    assert invalid_path.status_code == 422
    assert invalid_path.json() == {"detail": "invalid task ID"}

    for field in ("tenant_id", "principal_id", "thread_id", "claim", "lease_token"):
        response = client.post(
            f"/tasks/{task_id}/runs",
            headers=alice,
            json={"idempotency_key": "run-one", field: "forged"},
        )
        assert response.status_code == 422
    run_response = client.post(
        f"/tasks/{task_id}/runs", headers=alice, json={"idempotency_key": "run-one"}
    )
    assert run_response.status_code == 201
    run_id = run_response.json()["run_id"]
    leased = asyncio.run(repository.claim_next_run("unit-runner"))
    assert leased is not None and leased.lease_token is not None
    claimed = client.get(f"/tasks/{task_id}/runs/{run_id}", headers=alice)
    assert claimed.status_code == 200
    assert "lease_token" not in claimed.json()
    assert "thread_id" not in claimed.json()
    assert client.get(f"/tasks/{task_id}/runs/{run_id}", headers=bob).status_code == 404


def test_public_feedback_and_gate_bodies_reject_server_write_authority() -> None:
    class _Feedback:
        def __init__(self) -> None:
            self.commands = []
            self.gate = SimpleNamespace(
                interrupt_id="interrupt-1",
                kind=SystemInterruptKind.APPROVAL_REQUIRED,
                reason_code="user_gate",
                source=SystemInterruptSource(
                    module="workflow", graph="workflow", stage_key="cad2bim_workflow",
                    node="user_gate",
                ),
                question="Continue with the completed result?",
                context_summary="The current result is saved.",
                allowed_actions=("approve", "revise", "cancel"),
                checkpoint_cursor="private-address",
            )

        async def current_gate(self, subject, task_id, run_id):
            assert (task_id, run_id) == ("task-1", "run-1")
            return self.gate

        async def submit_feedback(self, subject, task_id, run_id, command):
            self.commands.append(command)
            return FeedbackCommandReceipt(
                feedback_id="feedback-1",
                accepted_sequence=1,
                delivery_mode=command.delivery_mode,
                resolved_scope=command.scope,
                status=FeedbackCommandStatus.ACCEPTED,
            )

        async def respond_gate(self, subject, task_id, run_id, command):
            self.commands.append(command)
            return SystemInterruptResponseReceipt(
                interrupt_id=command.interrupt_id,
                action=command.action,
                response_high_watermark=0,
                responded_at=datetime.now(timezone.utc),
            )

    private, public_pem = _keys()
    feedback = _Feedback()
    client = TestClient(
        create_task_api(
            authenticator=JwtRs256Authenticator(
                public_key_pem=public_pem, issuer=ISSUER, audience=AUDIENCE
            ),
            service=TaskRunService(
                repository=InMemoryControlPlaneRepository(),
                identity_directory=InMemoryIdentityDirectory(),
                execution_protocol_factory=_binding,
                bind_identity=bind_tenant_execution_context,
            ),
            feedback_service=feedback,
        )
    )
    headers = {"Authorization": f"Bearer {_token(private)}"}
    feedback_path = "/tasks/task-1/runs/run-1/feedback"
    body = {"client_request_id": "one", "delivery_mode": "steer", "content": [{"type": "text", "text": "Text"}]}
    for forbidden in ("writer_generation", "tenant_id", "principal_id", "thread_id", "claim"):
        assert client.post(
            feedback_path, headers=headers, json={**body, forbidden: "forged"}
        ).status_code == 422
    accepted = client.post(feedback_path, headers=headers, json=body)
    assert accepted.status_code == 201
    assert feedback.commands[0].scope.run_id == "run-1"
    assert "writer_generation" not in accepted.json()["resolved_scope"]

    gate_path = "/tasks/task-1/runs/run-1/gates/interrupt-1/respond"
    current_gate_path = "/tasks/task-1/runs/run-1/gates/current"
    assert client.get(current_gate_path).status_code == 401
    current_gate = client.get(current_gate_path, headers=headers)
    assert current_gate.status_code == 200
    assert current_gate.json()["interrupt_id"] == "interrupt-1"
    assert current_gate.json()["allowed_actions"] == ["approve", "revise", "cancel"]
    assert "checkpoint_cursor" not in current_gate.json()
    assert "runtime" not in current_gate.json()
    feedback.gate = None
    assert client.get(current_gate_path, headers=headers).status_code == 404
    gate_body = {"action": "approve", "idempotency_key": "once"}
    for forbidden in (
        "decision", "checkpoint_cursor", "response_high_watermark", "expected_writer_generation"
    ):
        assert client.post(
            gate_path, headers=headers, json={**gate_body, forbidden: "forged"}
        ).status_code == 422
    response = client.post(gate_path, headers=headers, json=gate_body)
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "approve"
    assert "response_high_watermark" not in response.json()
