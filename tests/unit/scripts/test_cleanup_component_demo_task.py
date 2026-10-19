from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from scripts.maintenance import cleanup_component_demo_task as cli
from CAD2BIM_backend.domain.product_family_authoring_contracts import ProductFamilyAuthoringVersionDraft
from tests.unit.backend.test_product_family_catalog_service import _family_bundle, _geometry


TASK = cli.DEFAULT_TASK_ID
SNAPSHOT = {"artifact_id": "artifact-test", "version_id": "version-test", "sha256": "a" * 64}


def write(kind, operation_id, *, root_id=None, task_id=TASK, status="succeeded", expected=None):
    root_id = root_id or cli.stable_id("geometry_resource" if kind == "geometry" else "product_family",
                                      {"task_id": task_id, "operation_id": operation_id})
    return SimpleNamespace(task_id=task_id, operation_id=operation_id, status=status,
        operation_kind="geometry_resource_write" if kind == "geometry" else "product_family_write",
        operation_detail_json={"detail_kind": "authoring_draft_write", "root_kind": kind,
            "root_id": root_id, "expected_generation": expected, "result_generation": (expected or 0) + 1})


def publish(writes, *, operation_id="publish-test", task_id=TASK, status="succeeded"):
    target = writes[0].operation_detail_json
    family = next((w for w in writes if w.operation_detail_json["root_kind"] == "family"), None)
    version = None
    if family:
        bundle = _family_bundle()
        old = bundle.version
        version = ProductFamilyAuthoringVersionDraft(**{
            name: getattr(old, name) for name in ProductFamilyAuthoringVersionDraft.model_fields if name != "geometry_bindings"
        }, geometry_bindings=[{**binding.model_dump(exclude={"family_version_id"}),
                              "geometry_owner_scope": _geometry().resource.owner_scope}
                             for binding in bundle.geometry_bindings]).model_dump(mode="json")
        version["family_id"] = family.operation_detail_json["root_id"]
    return SimpleNamespace(task_id=task_id, operation_id=operation_id, status=status,
        operation_kind="authoring_publish", operation_detail_json={
            "detail_kind": "authoring_publish", "root_kind": target["root_kind"], "root_id": target["root_id"],
            "expected_drafts": [{"root_kind": w.operation_detail_json["root_kind"],
                                 "root_id": w.operation_detail_json["root_id"], "generation": 1, "snapshot": SNAPSHOT}
                                for w in writes],
            "geometry_versions": [{"resource_id": w.operation_detail_json["root_id"], "resource_version_id": "version-" + w.operation_id}
                                  for w in writes if w.operation_detail_json["root_kind"] == "geometry"],
            "family_version": version})


def test_provenance_requires_own_new_creation_and_successful_publish_not_first_edit():
    family, geometry = write("family", "family-create"), write("geometry", "geometry-create")
    reused = write("geometry", "first-draft-of-existing-root", root_id="existing-tenant-root")
    system = write("geometry", "system-copy-reference", root_id="system-bend-root")
    foreign = write("geometry", "foreign-create", task_id="task_foreign")
    failed = write("geometry", "failed-create", status="failed")
    unpublished = write("geometry", "unpublished-create")
    revised = write("geometry", "revision", expected=1)
    records = [family, geometry, reused, system, foreign, failed, unpublished, revised,
               publish([family, geometry, reused, system, foreign, failed, revised])]
    roots = cli._new_published_roots(TASK, records)
    assert set(roots) == {("family", family.operation_detail_json["root_id"]),
                          ("geometry", geometry.operation_detail_json["root_id"])}
    assert roots[("geometry", geometry.operation_detail_json["root_id"])] == {
        "creation_operation_id": "geometry-create", "publication_operation_ids": ["publish-test"]}
    assert not cli._new_published_roots(TASK, [geometry, publish([geometry], status="failed")])


@pytest.mark.parametrize("runs,operations", [
    ([SimpleNamespace(status="queued", pause_requested=False)], []),
    ([SimpleNamespace(status="running", pause_requested=False)], []),
    ([SimpleNamespace(status="paused", pause_requested=True)], []),
    ([], [SimpleNamespace(status="prepared")]),
    ([], [SimpleNamespace(status="indeterminate")]),
])
def test_inflight_or_unresolved_action_is_rejected(runs, operations):
    with pytest.raises(cli.CleanupRejected):
        cli._assert_idle(runs, operations)


def test_geometry_consumers_retire_first_and_cycle_is_refused():
    assert cli._geometry_order({"input", "consumer"}, [("consumer", "input")]) == ["consumer", "input"]
    with pytest.raises(cli.CleanupRejected, match="cycle"):
        cli._geometry_order({"a", "b"}, [("a", "b"), ("b", "a")])


def plan():
    return {"task_id": TASK, "title": "带压力表过滤阀组产品族建模验证", "unavailable_roots": [],
            "resources": [{"kind": "family", "root_id": "new-family", "creation_operation_id": "f-write", "publication_operation_ids": ["pub"]},
                          {"kind": "geometry", "root_id": "new-geometry", "creation_operation_id": "g-write", "publication_operation_ids": ["pub"]}]}


@pytest.mark.asyncio
async def test_default_dry_run_never_calls_api_or_allocates_references():
    async def read():
        return plan()
    class NoApi:
        async def request(self, *args, **kwargs):
            pytest.fail("dry-run called HTTP API")
    async def refs(_resources):
        pytest.fail("dry-run allocated references")
    result = await cli.cleanup(read, NoApi(), refs)
    assert result["status"] == "planned" and not result["completed"]
    assert cli._parser().parse_args([]).task_id == TASK
    assert cli._parser().parse_args([]).execute is False


async def apply_with_transport(*, failure=None, new_resource=False, read_error=False):
    state, requests = plan(), []
    async def read():
        if read_error and not state["resources"]:
            raise RuntimeError("postgresql://do-not-print-credential")
        return deepcopy(state)
    async def refs(resources):
        return {(r["kind"], r["root_id"]): "fr_test" if r["kind"] == "family" else "gr_test" for r in resources}
    def handle(request):
        requests.append((request.method, request.url.path, dict(request.url.params)))
        if request.method == "DELETE" and request.url.path == "/library/geometry" and failure:
            if failure == "transport":
                raise httpx.ReadTimeout("secret-credential", request=request)
            return httpx.Response(failure, json={"detail": {"code": "LIBRARY_GEOMETRY_IN_USE"}})
        if request.method == "DELETE" and request.url.path.startswith("/library/"):
            kind = "family" if request.url.path.endswith("families") else "geometry"
            state["resources"] = [r for r in state["resources"] if r["kind"] != kind]
            if new_resource and not state["resources"]:
                state["resources"].append({"kind": "geometry", "root_id": "new-concurrent-root"})
        return httpx.Response(204 if request.method == "DELETE" else 200, json={} if request.method == "GET" else None)
    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        result = await cli.cleanup(read, client, refs, execute=True)
    return result, requests


@pytest.mark.asyncio
async def test_execute_calls_existing_family_geometry_task_apis_in_order():
    result, requests = await apply_with_transport()
    deletes = [r for r in requests if r[0] == "DELETE"]
    assert [r[1] for r in deletes] == ["/library/families", "/library/geometry", f"/tasks/{TASK}"]
    assert deletes[0][2] == {"reference": "fr_test"}
    assert deletes[1][2] == {"reference": "gr_test"}
    assert result["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [409, 403, 404, 302, "transport"])
async def test_rejection_or_unknown_response_stops_and_preserves_task_with_partial_report(failure):
    result, requests = await apply_with_transport(failure=failure)
    assert result["status"] == "stopped"
    assert result["completed"] == [{"kind": "family", "root_id": "new-family"}]
    assert result["current"]["root_id"] == "new-geometry"
    assert result["stage"] == "resource_delete" and result["delete_attempted"]
    assert not any(method == "DELETE" and path == f"/tasks/{TASK}" for method, path, _ in requests)
    assert "secret-credential" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"new_resource": True}, {"read_error": True}])
async def test_new_publication_or_read_failure_after_deletes_retains_task_and_completed_actions(options):
    result, requests = await apply_with_transport(**options)
    assert result["status"] == "stopped" and len(result["completed"]) == 2
    assert result["stage"] == "task_recheck" and not result["delete_attempted"]
    assert result["current"]["confirmation"] == "not_attempted"
    assert not any(method == "DELETE" and path == f"/tasks/{TASK}" for method, path, _ in requests)
    assert "do-not-print-credential" not in str(result)


@pytest.mark.asyncio
async def test_archived_or_unavailable_task_never_becomes_cleanup_success():
    async def read():
        raise cli.CleanupRejected("Task is unavailable or already archived.")
    with pytest.raises(cli.CleanupRejected, match="unavailable"):
        await cli.cleanup(read, None, None, execute=True)


def test_cli_never_exposes_credentials_from_auth_or_configuration_failures(monkeypatch, capsys):
    async def broken(_arguments):
        raise RuntimeError("postgresql://private-password")
    monkeypatch.setattr(cli, "run", broken)
    assert cli.main([]) == 1
    output = capsys.readouterr().out
    assert "private-password" not in output and '"status": "rejected"' in output


class ReadSession:
    def __init__(self, role):
        self.role, self.statements = role, []
    def begin(self):
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_args):
        pass
    async def execute(self, statement, *_args):
        self.statements.append(str(statement))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: self.role))
    async def scalar(self, _statement):
        pytest.fail("unsafe role proceeded to business reads")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"rolsuper": True}, {"rolbypassrls": True}, {"row_security": "off"}, {"api_role": False}])
async def test_privileged_or_non_api_role_refused_in_read_only_transaction(change):
    role = {"rolsuper": False, "rolbypassrls": False, "row_security": "on", "api_role": True, **change}
    session = ReadSession(role)
    database = SimpleNamespace(session_factory=lambda: session)
    identity = SimpleNamespace(tenant_id="tenant-test", principal_id="principal-test", task_id=TASK)
    with pytest.raises(cli.CleanupRejected, match="API runtime role"):
        await cli.read_plan(database, identity)
    assert session.statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert all(s.lstrip().startswith(("SELECT", "SET TRANSACTION")) for s in session.statements)


@pytest.mark.parametrize("kind,model,id_field", [
    ("geometry", cli.GeometryResourceModel, "resource_id"),
    ("family", cli.ProductFamilyModel, "family_id"),
])
def test_actual_plan_query_explicitly_filters_soft_deleted_roots(kind, model, id_field):
    sql = str(cli._live_root_statement(kind, "new-root").compile(dialect=postgresql.dialect()))
    where = sql.split("WHERE ", 1)[1]
    assert f"{model.__tablename__}.{id_field} = " in where
    assert f"{model.__tablename__}.deleted_at IS NULL" in where


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["file", "environment", "frontend"])
async def test_default_reuses_existing_credentials_with_explicit_precedence(source, monkeypatch, tmp_path):
    (tmp_path / "runtime.json").write_text(json.dumps({
        "issuer": "https://identity.test", "audience": "test-api", "api_database_url": "api-runtime-url",
        "admin_database_url": "forbidden-admin-url", "runner_database_url": "forbidden-runner-url"}))
    (tmp_path / "public.pem").write_bytes(b"existing-public-key")
    (tmp_path / "frontend.env").write_text("COMPONENT_BACKEND_BEARER=existing-ui-token\n")
    token_file = tmp_path / "existing-token"
    token_file.write_text("existing-explicit-token")
    if source != "frontend":
        monkeypatch.setenv(cli.TOKEN_ENV, "existing-environment-token")
    else:
        monkeypatch.delenv(cli.TOKEN_ENV, raising=False)
    captured = {}
    class Authenticator:
        def __init__(self, *, public_key_pem, issuer, audience):
            captured.update(public_key_pem=public_key_pem, issuer=issuer, audience=audience)
        def authenticate(self, token):
            captured["token"] = token
            return SimpleNamespace(issuer="https://identity.test", subject="original-user")
    monkeypatch.setattr(cli, "JwtRs256Authenticator", Authenticator)
    database = SimpleNamespace(dispose=lambda: dispose())
    async def dispose():
        captured["disposed"] = True
    def create_database(settings):
        captured["database_url"] = settings.database_url
        return database
    monkeypatch.setattr(cli, "create_database", create_database)
    async def read_plan(_database, identity):
        captured["identity"] = identity
        return plan()
    monkeypatch.setattr(cli, "read_plan", read_plan)
    arguments = ["--runtime-dir", str(tmp_path)]
    if source == "file":
        arguments += ["--token-file", str(token_file)]
    result = await cli.run(cli._parser().parse_args(arguments))
    assert result["status"] == "planned"
    assert captured["token"] == {"file": "existing-explicit-token", "environment": "existing-environment-token",
                                 "frontend": "existing-ui-token"}[source]
    assert captured["public_key_pem"] == b"existing-public-key"
    assert captured["database_url"] == "api-runtime-url" and captured["disposed"]
    assert captured["identity"].task_id == TASK
    assert captured["identity"].principal_id == cli.stable_internal_identity_id(
        "principal_", "authenticated-principal", "https://identity.test", "original-user")


def test_runtime_reference_key_uses_same_decoded_secret_as_existing_server():
    secret = b"an-isolated-existing-runtime-secret-for-the-test!!"
    codec = cli._runtime_cursor_codec({"state_secret": base64.b64encode(secret).decode()})
    server_key = base64.urlsafe_b64encode(hashlib.sha256(
        b"cad2bim-runner-ifc-cursor:" + secret).digest()).rstrip(b"=").decode()
    server = cli.CursorCodec(server_key)
    saved_reference_claims = {"domain": "geometry_catalog_reference", "reference_kind": "resource_root",
                             "resource_id": "isolated-new-root", "scope": "isolated-scope"}
    assert codec.decode(server.encode(saved_reference_claims)) == saved_reference_claims


@pytest.mark.asyncio
@pytest.mark.parametrize("task_scope", [False, True])
async def test_catalog_uses_actual_user_scope_sealed_refs_without_runner_index(task_scope):
    identity = cli.TenantExecutionContext(tenant_id="tenant-test", principal_id="principal-test",
        actor_kind=cli.TenantActorKind.USER, task_id=TASK, command_id="cleanup-test")
    codec = cli._runtime_cursor_codec({"state_secret": base64.b64encode(b"isolated-secret" * 4).decode()})
    scope = identity if task_scope else identity.model_copy(update={"task_id": None})
    family_ref = codec.encode({"domain": "product_family_catalog_reference", "reference_kind": "family_root",
        "family_id": "new-family", "scope": cli.catalog_scope_digest(scope)})
    geometry_ref = codec.encode({"domain": "geometry_catalog_reference", "reference_kind": "resource_root",
        "resource_id": "new-geometry", "scope": cli.catalog_scope_digest(scope)})
    requested = []
    def response(request):
        requested.append(request)
        if request.url.path == "/library/families":
            return httpx.Response(200, json={"families": [{"family_root_ref": family_ref}], "next_cursor": None})
        return httpx.Response(200, json={"resources": [{"resource_root_ref": geometry_ref}], "next_cursor": None})
    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(response)) as client:
        if task_scope:
            with pytest.raises(cli.CleanupRejected, match="USER Library scope"):
                await cli.catalog_references(client, plan()["resources"], identity, codec)
        else:
            result = await cli.catalog_references(client, plan()["resources"], identity, codec)
            assert result == {("family", "new-family"): family_ref, ("geometry", "new-geometry"): geometry_ref}
    assert all("task_id" not in request.url.params and request.method == "GET" for request in requested)


@pytest.mark.asyncio
async def test_task_scoped_root_is_explicitly_rejected_before_any_delete():
    geometry = write("geometry", "task-root-create")
    class TaskRootSession(ReadSession):
        def __init__(self):
            super().__init__({"rolsuper": False, "rolbypassrls": False, "row_security": "on", "api_role": True})
            self.scalar_results = iter(["principal-test", SimpleNamespace(task_id=TASK, title="Test"),
                SimpleNamespace(owner_scope="TASK", owner_tenant_id="tenant-test", owner_task_id=TASK)])
            self.list_results = iter([[], [geometry, publish([geometry])]])
        async def scalar(self, _statement):
            return next(self.scalar_results)
        async def scalars(self, _statement):
            values = next(self.list_results)
            return SimpleNamespace(all=lambda: values)
    database = SimpleNamespace(session_factory=lambda: TaskRootSession())
    identity = SimpleNamespace(tenant_id="tenant-test", principal_id="principal-test", task_id=TASK)
    with pytest.raises(cli.CleanupRejected, match="TENANT Library roots only"):
        await cli.read_plan(database, identity)


@pytest.mark.asyncio
async def test_reference_read_failure_reports_stage_and_no_delete_attempt():
    async def read():
        return plan()
    async def refs(_resources):
        raise RuntimeError("credential-that-must-not-be-printed")
    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={}))) as client:
        result = await cli.cleanup(read, client, refs, execute=True)
    assert result["status"] == "stopped" and result["stage"] == "catalog_references"
    assert result["completed"] == [] and not result["delete_attempted"]
    assert result["current"]["confirmation"] == "not_attempted"
    assert result["exception_type"] == "RuntimeError"
    assert "credential-that-must-not-be-printed" not in str(result)
