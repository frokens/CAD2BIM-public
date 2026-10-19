"""Compose the authenticated HTTP entry and the fenced Runner runtime."""

from __future__ import annotations

import base64
import inspect
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from langgraph.checkpoint.base import BaseCheckpointSaver

from CAD2BIM_backend.api import create_task_api
from CAD2BIM_backend.application.artifact_service import ArtifactService
from CAD2BIM_backend.application.feedback_service import FeedbackService
from CAD2BIM_backend.application.runner_execution_service import RunnerExecutionService
from CAD2BIM_backend.application.state_resource_service import StateResourceService
from CAD2BIM_backend.application.task_run_service import TaskRunService
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.data_application_resources import DataApplicationResources
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.ifc_application_resources import (
    AuthoringApplicationResources,
    IfcApplicationResources,
)
from CAD2BIM_backend.infrastructure.authentication import JwtRs256Authenticator
from CAD2BIM_backend.infrastructure.data_query.snapshot_cache import QuerySnapshotCache
from CAD2BIM_backend.infrastructure.langgraph.feedback_nodes import FeedbackNodes
from CAD2BIM_backend.infrastructure.langgraph.runtime import (
    AsyncPostgresGraphRuntime,
)
from CAD2BIM_backend.infrastructure.langgraph.user_gate import (
    build_run_pause_open_request,
    build_user_gate_open_request,
    reentry_route_update,
)
from CAD2BIM_backend.infrastructure.postgres.database import Database, create_database
from CAD2BIM_backend.infrastructure.postgres.identity_directory import PostgresIdentityDirectory
from CAD2BIM_backend.infrastructure.postgres.repository import PostgresControlPlaneRepository
from CAD2BIM_backend.infrastructure.postgres.tenant_context import bind_tenant_execution_context
from CAD2BIM_backend.infrastructure.runner_domain_tokens import RunnerDomainTokenCodec
from CAD2BIM_backend.infrastructure.state_access_tokens import StateAccessTokenCodec
from CAD2BIM_backend.infrastructure.storage import LocalStorage
from CAD2BIM_backend.internal_state_api import create_internal_state_router
from CAD2BIM_backend.ports.checkpoint_observers import DurableCheckpointObserver

FeedbackGraphBuilder = Callable[
    [BaseCheckpointSaver[str], FeedbackNodes], Any | Awaitable[Any]
]


@dataclass(slots=True)
class BackendRuntime:
    api: FastAPI
    runners: tuple[RunnerExecutionService, ...]
    graph_runtime: AsyncPostgresGraphRuntime
    api_database: Database
    runner_database: Database
    state_access_codec: StateAccessTokenCodec
    domain_access_codec: RunnerDomainTokenCodec
    state_resources: StateResourceService
    feedback_service: FeedbackService
    feedback_nodes: FeedbackNodes
    task_runs: TaskRunService
    repository: PostgresControlPlaneRepository
    artifact_service: ArtifactService | None = None
    chat_projection: Any = None
    ifc_resources: IfcApplicationResources | None = None
    authoring_resources: AuthoringApplicationResources | None = None
    data_resources: DataApplicationResources | None = None
    query_snapshots: QuerySnapshotCache | None = None

    @property
    def runner(self) -> RunnerExecutionService:
        """The first existing executor for explicitly single-Run callers."""
        return self.runners[0]

    async def _close_data(self) -> None:
        try:
            if self.query_snapshots is not None:
                await self.query_snapshots.close()
        finally:
            if self.data_resources is not None:
                try:
                    await self.data_resources.python_transform_worker.close()
                finally:
                    await self.data_resources.transform_worker.close()

    async def close(self) -> None:
        try:
            await self._close_data()
        finally:
            try:
                if self.authoring_resources is not None:
                    await self.authoring_resources.close()
            finally:
                try:
                    if self.ifc_resources is not None:
                        await self.ifc_resources.close()
                finally:
                    try:
                        await self.graph_runtime.close()
                    finally:
                        try:
                            await self.runner_database.dispose()
                        finally:
                            await self.api_database.dispose()


async def open_backend_runtime(
    *,
    api_settings: Settings,
    runner_settings: Settings,
    public_key_pem: bytes,
    issuer: str,
    audience: str,
    execution_protocol: ExecutionProtocolBinding,
    graph_builder: FeedbackGraphBuilder | None = None,
    runner_id: str,
    state_access_secret: bytes,
    checkpoint_observer: DurableCheckpointObserver | None = None,
    ifc_storage_root: Path | None = None,
    artifact_storage_root: Path | None = None,
    component_model: Any = None,
    component_tools_factory: Callable[..., Any] | None = None,
    component_skills: Any = None,
    component_initial_reference_text: str | None = None,
    component_input_limit: int = 256_000,
    component_output_reserve: int = 8_192,
    component_summary_input_limit: int = 256_000,
    recoverable_execution_error: Callable[[Exception], str | None] | None = None,
    runner_slots: int = 1,
) -> BackendRuntime:
    """Open separate API and Runner database identities on the current schema."""

    if isinstance(runner_slots, bool) or not isinstance(runner_slots, int) or not 1 <= runner_slots <= 4:
        raise ValueError("runner_slots must be an integer between 1 and 4")
    if component_model is not None and ifc_storage_root is None:
        raise ValueError("Component requires the shared authoring/domain storage and routes")
    if runner_settings.database_url is None:
        raise ValueError("Runner database URL is required")
    api_database = create_database(api_settings)
    try:
        runner_database = create_database(runner_settings)
    except BaseException:
        await api_database.dispose()
        raise
    graph_runtime: AsyncPostgresGraphRuntime | None = None
    ifc_resources: IfcApplicationResources | None = None
    authoring_resources: AuthoringApplicationResources | None = None
    data_resources: DataApplicationResources | None = None
    query_snapshots: QuerySnapshotCache | None = None
    try:
        await api_database.verify_ready()
        await runner_database.verify_ready()
        api_repository = PostgresControlPlaneRepository(api_database.session_factory)
        runner_repository = PostgresControlPlaneRepository(runner_database.session_factory)
        identities = PostgresIdentityDirectory(api_database.session_factory)
        storage_root = artifact_storage_root or ifc_storage_root
        storage = LocalStorage(storage_root, max_size_bytes=runner_settings.max_upload_bytes) if storage_root else None
        artifacts = ArtifactService(runner_repository, storage) if storage is not None else None
        if storage is not None:
            await storage.verify_ready()
        runner_feedback_service = FeedbackService(
            repository=runner_repository,
            identity_directory=identities,
            bind_identity=bind_tenant_execution_context,
            artifacts=artifacts,
        )
        feedback_nodes = FeedbackNodes(runner_feedback_service)
        runner_task_runs = TaskRunService(
            repository=runner_repository, identity_directory=identities,
            execution_protocol_factory=lambda module, stage_key: _current_protocol(
                execution_protocol, module=module, stage_key=stage_key,
            ),
            bind_identity=bind_tenant_execution_context,
        )

        async def build_graph(saver: BaseCheckpointSaver[str]) -> Any:
            if component_model is not None:
                from CAD2BIM_backend.infrastructure.langgraph.component_graph import (
                    COMPONENT_PROMPT,
                    build_component_graph,
                )
                from CAD2BIM_backend.infrastructure.langgraph.role_graph import RoleProfile

                if artifacts is None:
                    raise ValueError("Component requires its existing Artifact storage")
                bindings, executor = ({}, None)
                if component_tools_factory is not None:
                    prepared = component_tools_factory(artifacts, feedback_nodes)
                    bindings, executor = await prepared if inspect.isawaitable(prepared) else prepared
                profile = RoleProfile(
                    role="component", core_prompt=COMPONENT_PROMPT, model=component_model,
                    tools=bindings, skills=component_skills,
                    input_limit=component_input_limit, output_reserve=component_output_reserve,
                    summary_input_limit=component_summary_input_limit,
                )
                return build_component_graph(
                    saver, feedback_nodes, profile=profile, artifacts=artifacts, tool_executor=executor,
                    task_runs=runner_task_runs,
                    grant_provider=lambda: graph_runtime.require_current_execution_grant(),
                    initial_reference_text=component_initial_reference_text,
                )
            if graph_builder is None:
                raise ValueError("a graph builder is required")
            built = graph_builder(saver, feedback_nodes)
            return await built if inspect.isawaitable(built) else built

        graph_runtime = await AsyncPostgresGraphRuntime.open(
            database_url=runner_settings.database_url,
            graph_builder=build_graph,
            checkpoint_observer=checkpoint_observer,
            execution_protocol=execution_protocol,
        )
        feedback_nodes.bind_grant_provider(graph_runtime.require_current_execution_grant)
        task_runs = TaskRunService(
            repository=api_repository,
            identity_directory=identities,
            execution_protocol_factory=lambda module, stage_key: _current_protocol(
                execution_protocol, module=module, stage_key=stage_key
            ),
            bind_identity=bind_tenant_execution_context,
            graph_runtime=graph_runtime,
            observation_repository=runner_repository,
        )
        authenticator = JwtRs256Authenticator(
            public_key_pem=public_key_pem, issuer=issuer, audience=audience
        )
        state_access_codec = StateAccessTokenCodec(state_access_secret)
        domain_access_codec = RunnerDomainTokenCodec(state_access_secret)
        state_resources = StateResourceService(graph_runtime)
        feedback_service = FeedbackService(
            repository=api_repository,
            identity_directory=identities,
            bind_identity=bind_tenant_execution_context,
            artifacts=artifacts,
        )
        chat_projection = None
        if component_model is not None:
            from CAD2BIM_backend.infrastructure.chat_stream import create_component_presentation

            chat_projection = create_component_presentation(
                task_runs=task_runs, repository=runner_repository,
                graph_runtime=graph_runtime, bind_identity=bind_tenant_execution_context,
                feedback_repository=api_repository, artifact_service=artifacts,
            )
            if checkpoint_observer is not None:
                raise ValueError("Component presentation owns its checkpoint observer")
            graph_runtime.bind_checkpoint_observer(chat_projection.checkpoint_observer)
            graph_runtime.bind_callbacks([chat_projection.callback_factory()])
        api = create_task_api(
            authenticator=authenticator,
            service=task_runs,
            state_access_codec=state_access_codec,
            feedback_service=feedback_service,
            artifact_service=artifacts,
            bind_identity=bind_tenant_execution_context,
            chat_projection=chat_projection,
        )
        api.include_router(
            create_internal_state_router(
                codec=state_access_codec,
                resources=state_resources,
                task_runs=task_runs,
                runner_repository=runner_repository,
                bind_identity=bind_tenant_execution_context,
            )
        )
        if ifc_storage_root is not None:
            from CAD2BIM_backend.application.exact_payload_resolver import ExactPayloadResolver
            from CAD2BIM_backend.data_application_resources import create_data_application_resources
            from CAD2BIM_backend.ifc_application_resources import (
                create_authoring_application_resources,
                create_ifc_application_resources,
                connect_product_apply_service,
                connect_product_edit_service,
                connect_product_read_service,
            )
            from CAD2BIM_backend.infrastructure.data_query import CursorCodec
            from CAD2BIM_backend.infrastructure.ifc.codec import MAX_IFC_REVISION_BYTES
            from CAD2BIM_backend.infrastructure.source_text import TextSourcePatchAdapter
            from CAD2BIM_backend.infrastructure.worker_runtime import HostWorkerCapacity
            from CAD2BIM_backend.infrastructure.worker_runtime.capacity import default_capacity_root
            from CAD2BIM_backend.internal_domain_api import create_internal_domain_router

            storage = LocalStorage(
                ifc_storage_root,
                max_size_bytes=max(runner_settings.max_upload_bytes, MAX_IFC_REVISION_BYTES),
            )
            artifacts = ArtifactService(runner_repository, storage)
            cursor_key = base64.urlsafe_b64encode(
                sha256(b"cad2bim-runner-ifc-cursor:" + state_access_secret).digest()
            ).rstrip(b"=").decode("ascii")
            capacity = HostWorkerCapacity(max_concurrent_workers=1)
            sql_capacity = HostWorkerCapacity(
                max_concurrent_workers=2,
                capacity_root=default_capacity_root(pool="sql"),
                resource_pool="sql",
            )
            payload_resolver = ExactPayloadResolver(runner_repository, artifacts)
            cursor_codec = CursorCodec(cursor_key)
            source_patches = TextSourcePatchAdapter()
            ifc_resources = create_ifc_application_resources(
                repository=runner_repository,
                storage=storage,
                artifacts=artifacts,
                payloads=payload_resolver,
                cursor_codec=cursor_codec,
                source_patches=source_patches,
                capacity=capacity,
            )
            authoring_resources = create_authoring_application_resources(
                repository=runner_repository,
                storage=storage,
                artifacts=artifacts,
                cursor_codec=cursor_codec,
                capacity=capacity,
            )
            connect_product_apply_service(ifc=ifc_resources, authoring=authoring_resources,
                repository=runner_repository, payloads=payload_resolver, storage=storage)
            query_snapshots = QuerySnapshotCache()
            data_resources = create_data_application_resources(
                repository=runner_repository,
                artifacts=artifacts,
                cursor_codec=cursor_codec,
                payload_resolver=payload_resolver,
                source_patch_adapter=source_patches,
                query_snapshots=query_snapshots,
                sql_capacity=sql_capacity,
                python_capacity=capacity,
                python_worker_executable=sys.executable,
                geometry_resources=authoring_resources.geometry_resources,
                geometry_catalog=authoring_resources.geometry_catalog,
                geometry_sources=authoring_resources.geometry_source,
                geometry_queries=authoring_resources.geometry_query,
                ifc_queries=ifc_resources.query,
                ifc_revisions=ifc_resources.revision_reader,
                ifc_worker=ifc_resources.query_worker,
                geometry_authoring=authoring_resources.geometry_authoring,
                product_family_authoring=authoring_resources.family_authoring,
                product_family_catalog=authoring_resources.family_catalog,
                product_family_assets=authoring_resources.family_assets,
                source_bundles=authoring_resources.source_bundles,
            )
            connect_product_read_service(ifc=ifc_resources, data_runtime=data_resources.runtime,
                repository=runner_repository, payloads=payload_resolver)
            connect_product_edit_service(ifc=ifc_resources, authoring=authoring_resources,
                repository=runner_repository, payloads=payload_resolver, storage=storage,
                references=data_resources.references)
            await storage.verify_ready()
            if component_model is not None:
                from CAD2BIM_backend.application.saved_geometry_display_service import SavedGeometryDisplayService
                from CAD2BIM_backend.saved_geometry_display_api import create_saved_geometry_display_router

                saved_display = SavedGeometryDisplayService(
                    artifacts=artifacts, geometry_runtime=authoring_resources.geometry_runtime,
                    geometry_catalog=authoring_resources.geometry_catalog,
                    geometry_resources=authoring_resources.geometry_resources,
                    family_catalog=authoring_resources.family_catalog,
                    family_assets=authoring_resources.family_assets,
                    inspections=authoring_resources.geometry_inspection,
                )
                api.include_router(create_saved_geometry_display_router(
                    authenticator=authenticator, task_runs=task_runs,
                    references=data_resources.references,
                    bind_identity=bind_tenant_execution_context,
                    display=saved_display,
                ))
                from CAD2BIM_backend.library_api import create_library_router

                api.include_router(create_library_router(
                    authenticator=authenticator, task_runs=task_runs, identity_directory=identities,
                    geometry_catalog=authoring_resources.geometry_catalog,
                    family_catalog=authoring_resources.family_catalog,
                    code_discovery=data_resources.code_discovery,
                    saved_display=saved_display,
                    references=data_resources.references, bind_identity=bind_tenant_execution_context,
                ))
            api.include_router(
                create_internal_domain_router(
                    codec=domain_access_codec,
                    runner_repository=runner_repository,
                    bind_identity=bind_tenant_execution_context,
                    execution_protocol=execution_protocol,
                    data=data_resources,
                    ifc=ifc_resources,
                    authoring=authoring_resources,
                )
            )
        runners = tuple(RunnerExecutionService(
            repository=runner_repository,
            graph_runtime=graph_runtime,
            feedback_service=runner_feedback_service,
            build_gate_open_request=build_user_gate_open_request,
            build_pause_open_request=build_run_pause_open_request,
            build_gate_reentry_update=lambda plan, clarifications: reentry_route_update(
                plan, clarifications=clarifications
            ),
            bind_execution_context=bind_tenant_execution_context,
            runner_id=runner_id if runner_slots == 1 else f"{runner_id}-slot-{slot}",
            recoverable_execution_error=recoverable_execution_error,
        ) for slot in range(1, runner_slots + 1))
    except BaseException:
        try:
            if query_snapshots is not None:
                await query_snapshots.close()
        finally:
            try:
                if data_resources is not None:
                    try:
                        await data_resources.python_transform_worker.close()
                    finally:
                        await data_resources.transform_worker.close()
            finally:
                try:
                    if authoring_resources is not None:
                        await authoring_resources.close()
                finally:
                    try:
                        if ifc_resources is not None:
                            await ifc_resources.close()
                    finally:
                        try:
                            if graph_runtime is not None:
                                await graph_runtime.close()
                        finally:
                            try:
                                await runner_database.dispose()
                            finally:
                                await api_database.dispose()
        raise
    return BackendRuntime(
        api=api,
        runners=runners,
        graph_runtime=graph_runtime,
        api_database=api_database,
        runner_database=runner_database,
        state_access_codec=state_access_codec,
        domain_access_codec=domain_access_codec,
        state_resources=state_resources,
        feedback_service=feedback_service,
        feedback_nodes=feedback_nodes,
        task_runs=task_runs,
        repository=runner_repository,
        artifact_service=artifacts,
        chat_projection=chat_projection,
        ifc_resources=ifc_resources,
        authoring_resources=authoring_resources,
        data_resources=data_resources,
        query_snapshots=query_snapshots,
    )


def _current_protocol(
    execution_protocol: ExecutionProtocolBinding, *, module: str, stage_key: str
) -> ExecutionProtocolBinding:
    execution_protocol.require_scope(module=module, stage_key=stage_key)
    return execution_protocol


__all__ = ["BackendRuntime", "open_backend_runtime"]
