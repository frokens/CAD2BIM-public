"""AsyncPostgresSaver-backed implementation of the Graph runtime port."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, aclosing, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy.engine import make_url

from CAD2BIM_backend.domain.runner_context import RunnerCheckpointGrant

from CAD2BIM_backend.ports.graph_runtime import (
    GraphCheckpointDescriptor,
    GraphCheckpointState,
    GraphExecutionResult,
)
from CAD2BIM_backend.ports.checkpoint_observers import DurableCheckpointObserver
from CAD2BIM_backend.domain.execution_protocol import (
    ExecutionProtocolBinding,
)

from .execution_cursor import project_checkpoint_cursor
from .checkpoint_lineage import describe_checkpoint_lineage
from .observing_saver import ObservingAsyncCheckpointSaver
from .fenced_saver import FencedAsyncPostgresSaver


GraphBuilder = Callable[[BaseCheckpointSaver[str]], Any | Awaitable[Any]]

_OPERATION_ID_METADATA = "cad2bim_resume_operation_id"
_TARGET_RUN_ID_METADATA = "cad2bim_target_run_id"
_SOURCE_CHECKPOINT_ID_METADATA = "cad2bim_source_checkpoint_id"


@dataclass(frozen=True, slots=True)
class _OperationReceipt:
    checkpoint: Any
    snapshot: Any
    root_checkpoint_id: str


def _psycopg_connection_string(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("checkpoint database URL must use PostgreSQL/psycopg")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


class AsyncPostgresGraphRuntime:
    """Own one saver connection and a Graph compiled against that saver."""

    def __init__(
        self,
        *,
        graph: Any,
        saver: BaseCheckpointSaver[str],
        saver_context: AbstractAsyncContextManager[AsyncPostgresSaver],
        fenced_saver: FencedAsyncPostgresSaver | None = None,
        database_url: str | None = None,
        execution_protocol: ExecutionProtocolBinding | None = None,
        callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> None:
        self._graph = graph
        self._saver = saver
        self._fenced_saver = fenced_saver
        self._saver_context = saver_context
        self._database_url = database_url
        self._execution_protocol = execution_protocol
        self._callbacks = tuple(callbacks)
        self._execution_started = False
        self._closed = False

    def bind_checkpoint_observer(self, observer: DurableCheckpointObserver) -> None:
        """Finish observer composition before the first graph write."""

        self._require_open()
        if self._execution_started:
            raise RuntimeError("checkpoint observer must be bound before execution")
        if not isinstance(self._saver, ObservingAsyncCheckpointSaver):
            raise RuntimeError("runtime was not opened with the observing saver")
        self._saver.bind_observer(observer)

    def bind_callbacks(self, callbacks: Sequence[BaseCallbackHandler]) -> None:
        """Finish ordinary callback composition before the first graph write."""

        self._require_open()
        if self._execution_started:
            raise RuntimeError("callbacks must be bound before execution")
        if self._callbacks:
            raise RuntimeError("runtime callbacks are already bound")
        self._callbacks = tuple(callbacks)

    @property
    def execution_protocol(self) -> ExecutionProtocolBinding:
        if self._execution_protocol is None:
            raise RuntimeError("Runner Graph has no execution binding")
        return self._execution_protocol

    def require_current_execution_grant(self) -> RunnerCheckpointGrant:
        """Return the private claim bound to the active Graph invocation."""

        if self._fenced_saver is None:
            raise RuntimeError("durable Graph execution requires a fenced saver")
        return self._fenced_saver.require_current_execution_grant()

    @classmethod
    async def open(
        cls,
        *,
        database_url: str,
        graph_builder: GraphBuilder,
        setup_schema: bool = False,
        checkpoint_observer: DurableCheckpointObserver | None = None,
        execution_protocol: ExecutionProtocolBinding,
        callbacks: Sequence[BaseCallbackHandler] = (),
    ) -> "AsyncPostgresGraphRuntime":
        if setup_schema:
            raise RuntimeError("Runner cannot change the checkpoint schema")
        saver_context = FencedAsyncPostgresSaver.from_conn_string(
            _psycopg_connection_string(database_url)
        )
        saver = await saver_context.__aenter__()
        try:
            await saver.aget_tuple({"configurable": {"thread_id": "__cad2bim_readiness__"}})
            graph_saver = ObservingAsyncCheckpointSaver(saver, checkpoint_observer)
            graph = graph_builder(graph_saver)
            if inspect.isawaitable(graph):
                graph = await graph
        except BaseException:
            await saver_context.__aexit__(None, None, None)
            raise
        return cls(
            graph=graph,
            saver=graph_saver,
            saver_context=saver_context,
            fenced_saver=saver,
            database_url=database_url,
            execution_protocol=execution_protocol,
            callbacks=callbacks,
        )

    async def start(
        self,
        *,
        thread_id: str,
        input_value: Any,
        checkpoint_ns: str = "",
    ) -> GraphExecutionResult:
        raise RuntimeError("volatile Graph writes are unavailable to Runner")

    async def fork_exact_checkpoint(
        self,
        *,
        source: GraphCheckpointDescriptor,
        target_thread_id: str,
        operation_id: str,
        target_run_id: str,
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphCheckpointDescriptor:
        """Reject checkpoint forking until it has a claim-bound write contract."""

        raise RuntimeError("checkpoint fork is unavailable to Runner")

    async def start_durable(
        self,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        operation_id: str,
        target_run_id: str,
        input_value: Any,
        checkpoint_ns: str = "",
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphExecutionResult:
        """Start a Run exactly once at the Graph command boundary.

        This is an at-least-once process protocol: LangGraph checkpoints tagged
        with the stable Run operation are the receipt used after a Runner crash.
        Nodes before the first durable checkpoint must remain side-effect free.
        """

        self._require_open()
        self._require_grant(grant, thread_id=thread_id, target_run_id=target_run_id)
        if not operation_id.strip():
            raise ValueError("operation_id must not be empty")
        if not target_run_id.strip():
            raise ValueError("target_run_id must not be empty")
        metadata = self._operation_metadata(
            operation_id=operation_id,
            target_run_id=target_run_id,
            source_checkpoint_id="__start__",
        )
        if lease_guard is not None:
            await lease_guard()
        observed = await self._operation_receipt(
            thread_id=thread_id,
            metadata=metadata,
        )
        if observed is not None:
            return await self._reconcile_operation_receipt(
                observed,
                grant=grant,
                thread_id=thread_id,
                metadata=metadata,
            )
        config = self._config(
            thread_id,
            checkpoint_ns=checkpoint_ns,
            metadata=metadata,
        )
        await self._invoke(grant, input_value, config)
        completed = await self._operation_receipt(
            thread_id=thread_id,
            metadata=metadata,
        )
        if completed is None:
            raise RuntimeError("LangGraph start produced no operation checkpoint")
        return await self._reconcile_operation_receipt(
            completed,
            grant=grant,
            thread_id=thread_id,
            metadata=metadata,
        )

    async def resume(
        self,
        *,
        thread_id: str,
        resume_value: Any,
        state_update: Any | None = None,
        checkpoint_id: str | None = None,
        checkpoint_ns: str = "",
    ) -> GraphExecutionResult:
        raise RuntimeError("volatile Graph writes are unavailable to Runner")

    async def resume_durable(
        self,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        source_checkpoint_id: str,
        operation_id: str,
        target_run_id: str,
        resume_value: Any,
        state_update: Any | None = None,
        checkpoint_ns: str = "",
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphExecutionResult:
        """Execute or reconcile one logical resume operation.

        LangGraph checkpoints are the durable execution receipt.  The control
        plane may crash after Graph execution and before recording its result;
        on replay we find the newest checkpoint carrying this operation's
        metadata and never send ``Command(resume=...)`` a second time.
        """

        self._require_open()
        self._require_grant(grant, thread_id=thread_id, target_run_id=target_run_id)
        if not source_checkpoint_id.strip():
            raise ValueError("source_checkpoint_id must not be empty")
        if not operation_id.strip():
            raise ValueError("operation_id must not be empty")
        if not target_run_id.strip():
            raise ValueError("target_run_id must not be empty")

        metadata = self._operation_metadata(
            operation_id=operation_id,
            target_run_id=target_run_id,
            source_checkpoint_id=source_checkpoint_id,
        )
        if lease_guard is not None:
            await lease_guard()

        observed = await self._operation_receipt(
            thread_id=thread_id,
            metadata=metadata,
        )
        if observed is not None:
            return await self._reconcile_operation_receipt(
                observed,
                grant=grant,
                thread_id=thread_id,
                metadata=metadata,
            )

        source_snapshot = await self._graph.aget_state(
            self._config(
                thread_id,
                checkpoint_id=source_checkpoint_id,
                checkpoint_ns=checkpoint_ns,
            )
        )
        source = self._checkpoint_descriptor(
            source_snapshot,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            exact_checkpoint_id=source_checkpoint_id,
        )
        if source is None:
            raise RuntimeError("resume source checkpoint does not exist")
        config, command_update = await self._prepare_resume_command(
            source_snapshot,
            grant=grant,
            state_update=state_update,
            metadata=metadata,
        )
        await self._invoke(
            grant,
            Command(resume=resume_value, update=command_update),
            config,
        )
        completed = await self._operation_receipt(
            thread_id=thread_id,
            metadata=metadata,
        )
        if completed is None:
            raise RuntimeError("LangGraph resume produced no operation checkpoint")
        return await self._reconcile_operation_receipt(
            completed,
            grant=grant,
            thread_id=thread_id,
            metadata=metadata,
        )

    async def checkpoint_state(
        self,
        *,
        thread_id: str,
        values: Any,
        checkpoint_id: str | None = None,
        checkpoint_ns: str = "",
        as_node: str | None = None,
    ) -> GraphCheckpointDescriptor:
        """Persist values and return the checkpoint created by this exact update."""

        raise RuntimeError("volatile Graph writes are unavailable to Runner")

    async def checkpoint_state_durable(
        self,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        source_checkpoint_id: str,
        operation_id: str,
        target_run_id: str,
        values: Any,
        checkpoint_ns: str = "",
        as_node: str | None = None,
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphCheckpointDescriptor:
        """Persist one state update and reconcile its exact operation receipt."""

        self._require_open()
        self._require_grant(grant, thread_id=thread_id, target_run_id=target_run_id)
        if not source_checkpoint_id.strip():
            raise ValueError("source_checkpoint_id must not be empty")
        if not operation_id.strip():
            raise ValueError("operation_id must not be empty")
        if not target_run_id.strip():
            raise ValueError("target_run_id must not be empty")
        metadata = self._operation_metadata(
            operation_id=operation_id,
            target_run_id=target_run_id,
            source_checkpoint_id=source_checkpoint_id,
        )
        if lease_guard is not None:
            await lease_guard()
        observed = await self._operation_receipt(
            thread_id=thread_id,
            metadata=metadata,
        )
        if observed is not None:
            return self._operation_checkpoint_descriptor(
                observed,
                thread_id=thread_id,
            )
        source_snapshot = await self._graph.aget_state(
            self._config(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                checkpoint_id=source_checkpoint_id,
            )
        )
        source = self._checkpoint_descriptor(
            source_snapshot,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            exact_checkpoint_id=source_checkpoint_id,
        )
        if source is None:
            raise RuntimeError("state update source checkpoint does not exist")
        await self._update_state(
            grant,
            self._config(
                thread_id,
                checkpoint_id=source_checkpoint_id,
                checkpoint_ns=checkpoint_ns,
                metadata=metadata,
            ),
            values,
            as_node=as_node,
        )
        completed = await self._operation_receipt(
            thread_id=thread_id,
            metadata=metadata,
        )
        if completed is None:
            raise RuntimeError("LangGraph state update produced no operation checkpoint")
        return self._operation_checkpoint_descriptor(
            completed,
            thread_id=thread_id,
        )

    async def reconcile_durable_resume(
        self,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        source_checkpoint_id: str,
        operation_id: str,
        target_run_id: str,
        checkpoint_ns: str = "",
        continue_pending: bool = False,
        lease_guard: Callable[[], Awaitable[None]] | None = None,
    ) -> GraphExecutionResult | None:
        """Probe a prior durable resume before any MCP restore is replayed."""

        self._require_open()
        self._require_grant(grant, thread_id=thread_id, target_run_id=target_run_id)
        if not source_checkpoint_id.strip():
            raise ValueError("source_checkpoint_id must not be empty")
        if not operation_id.strip():
            raise ValueError("operation_id must not be empty")
        if not target_run_id.strip():
            raise ValueError("target_run_id must not be empty")
        metadata = self._operation_metadata(
            operation_id=operation_id,
            target_run_id=target_run_id,
            source_checkpoint_id=source_checkpoint_id,
        )
        if lease_guard is not None:
            await lease_guard()
        observed = await self._operation_receipt(
            thread_id=thread_id,
            metadata=metadata,
        )
        if observed is None:
            return None
        if not continue_pending:
            return await self._operation_result(observed, thread_id=thread_id)
        return await self._reconcile_operation_receipt(
            observed,
            grant=grant,
            thread_id=thread_id,
            metadata=metadata,
        )

    async def get_checkpoint(
        self,
        *,
        thread_id: str,
        checkpoint_id: str,
        checkpoint_ns: str = "",
    ) -> GraphCheckpointDescriptor | None:
        state = await self.get_checkpoint_state(
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
        )
        return state.checkpoint if state is not None else None

    async def get_checkpoint_state(
        self,
        *,
        thread_id: str,
        checkpoint_id: str,
        checkpoint_ns: str = "",
        observe: bool = True,
    ) -> GraphCheckpointState | None:
        self._require_open()
        if not checkpoint_id.strip():
            raise ValueError("get_checkpoint_state requires an exact checkpoint_id")
        config = self._config(
            thread_id,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
        )
        observation = (
            self._saver.without_read_observation()
            if not observe and isinstance(self._saver, ObservingAsyncCheckpointSaver) else nullcontext()
        )
        with observation:
            snapshot = await self._graph.aget_state(config)
        if snapshot.created_at is None:
            return None
        descriptor = self._checkpoint_descriptor(
            snapshot,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            exact_checkpoint_id=checkpoint_id,
        )
        if descriptor is None:
            raise RuntimeError("exact checkpoint has no descriptor")
        metadata = snapshot.metadata if isinstance(snapshot.metadata, Mapping) else {}
        return GraphCheckpointState(
            values=snapshot.values,
            checkpoint=descriptor,
            interrupts=tuple(
                {"id": str(getattr(item, "id", "")), "value": getattr(item, "value", None)}
                for item in tuple(snapshot.interrupts or ())
            ),
            graph_operation_id=self._metadata_text(metadata, _OPERATION_ID_METADATA),
            target_run_id=self._metadata_text(metadata, _TARGET_RUN_ID_METADATA),
        )

    async def close(self) -> None:
        if self._closed:
            return
        await self._saver_context.__aexit__(None, None, None)
        self._closed = True

    @staticmethod
    def _config(
        thread_id: str,
        *,
        checkpoint_id: str | None = None,
        checkpoint_ns: str = "",
        metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        if len(checkpoint_ns) > 1_024:
            raise ValueError("checkpoint_ns must be at most 1024 characters")
        configurable = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        config: dict[str, Any] = {"configurable": configurable}
        if metadata is not None:
            config["metadata"] = dict(metadata)
        return config

    def _operation_metadata(
        self,
        *,
        operation_id: str,
        target_run_id: str,
        source_checkpoint_id: str,
    ) -> dict[str, str]:
        return {
            _OPERATION_ID_METADATA: operation_id,
            _TARGET_RUN_ID_METADATA: target_run_id,
            _SOURCE_CHECKPOINT_ID_METADATA: source_checkpoint_id,
        }

    @staticmethod
    def _operation_command_config(
        snapshot: Any,
        *,
        metadata: Mapping[str, str],
    ) -> dict[str, Any]:
        if not snapshot.config:
            raise RuntimeError("operation source checkpoint cannot be read")
        configurable = dict(snapshot.config.get("configurable", {}))
        if not configurable.get("checkpoint_id"):
            raise RuntimeError("operation source checkpoint has no checkpoint_id")
        return {
            "configurable": configurable,
            "metadata": dict(metadata),
        }

    async def _prepare_resume_command(
        self,
        source_snapshot: Any,
        *,
        grant: RunnerCheckpointGrant,
        state_update: Any | None,
        metadata: Mapping[str, str],
    ) -> tuple[dict[str, Any], Any | None]:
        """Prepare an exact-branch resume without creating a sibling subgraph.

        A nested checkpoint identifies the interrupted leaf, but the compiled
        root graph must receive ``Command(resume=...)`` from the root cursor in
        that leaf's checkpoint map.  Invoking the root graph with the leaf
        config treats the subgraph call as a new invocation and can repeat
        already completed Planner/Modeler work under a new namespace.

        LangGraph applies ``Command(update=...)`` to the parent when a parent
        graph is invoked with a nested child checkpoint.  The interrupted
        child would therefore continue with stale Run/session identity and can
        emit writes under the source Run.  Persisting the pure state update on
        the exact child cursor first makes the rebind visible to the resumed
        node and lets the child propagate it back to the parent on completion.

        The preparatory checkpoint deliberately carries no operation receipt
        metadata.  A crash between this pure checkpoint write and ``ainvoke``
        may leave an unused branch, but replay still starts from the immutable
        source cursor and cannot mistake preparation for a consumed interrupt.
        """

        config = self._operation_command_config(source_snapshot, metadata=metadata)
        if state_update is None:
            return await self._root_resume_command_config(config), None
        configurable = config["configurable"]
        if not str(configurable.get("checkpoint_ns") or ""):
            return config, state_update
        if not isinstance(state_update, Mapping):
            raise ValueError("nested checkpoint state_update must be an object")

        source_config: dict[str, Any] = {"configurable": dict(configurable)}
        updated_config = await self._update_state(
            grant,
            source_config,
            dict(state_update),
        )
        updated_snapshot = await self._graph.aget_state(updated_config)
        updated = self._operation_command_config(updated_snapshot, metadata=metadata)
        if updated["configurable"].get("checkpoint_ns") != configurable.get("checkpoint_ns"):
            raise RuntimeError("nested state rebind changed checkpoint namespace")
        return updated, None

    async def _root_resume_command_config(
        self,
        leaf_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        configurable = dict(leaf_config.get("configurable", {}))
        checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
        if not checkpoint_ns:
            return dict(leaf_config)

        thread_id = str(configurable.get("thread_id") or "")
        checkpoint_map = configurable.get("checkpoint_map")
        if not isinstance(checkpoint_map, Mapping):
            raise RuntimeError("nested operation checkpoint has no checkpoint_map")
        root_checkpoint_id = str(checkpoint_map.get("") or "")
        if not root_checkpoint_id:
            raise RuntimeError("nested operation checkpoint has no root checkpoint")

        root_read_config = self._config(
            thread_id,
            checkpoint_id=root_checkpoint_id,
            checkpoint_ns="",
        )
        root_snapshot = await self._graph.aget_state(root_read_config)
        root_config = self._operation_command_config(
            root_snapshot,
            metadata=dict(leaf_config.get("metadata", {})),
        )
        exact = root_config["configurable"]
        if str(exact.get("checkpoint_ns") or ""):
            raise RuntimeError("root resume checkpoint has a nested namespace")
        if str(exact.get("checkpoint_id") or "") != root_checkpoint_id:
            raise RuntimeError("root resume checkpoint changed during exact read")
        return root_config

    async def _operation_receipt(
        self,
        *,
        thread_id: str,
        metadata: Mapping[str, str],
    ) -> _OperationReceipt | None:
        config = {"configurable": {"thread_id": thread_id}}
        # ``AsyncPostgresSaver.alist`` holds its internal connection lock until
        # the iterator is closed.  Read and close it before ``aget_state`` uses
        # the same saver, otherwise a single-connection runtime deadlocks itself.
        async with aclosing(self._saver.alist(config, filter=dict(metadata))) as checkpoints:
            matches = [checkpoint async for checkpoint in checkpoints]
        if not matches:
            return None
        checkpoint = self._latest_operation_checkpoint(matches)
        exact = checkpoint.config.get("configurable", {})
        checkpoint_id = exact.get("checkpoint_id")
        if not checkpoint_id:
            raise RuntimeError("operation checkpoint has no checkpoint_id")
        snapshot = await self._graph.aget_state(checkpoint.config)
        return _OperationReceipt(
            checkpoint=checkpoint,
            snapshot=snapshot,
            root_checkpoint_id=self._operation_root_checkpoint_id(
                checkpoint,
                snapshot,
            ),
        )

    def _operation_checkpoint_descriptor(
        self,
        receipt: _OperationReceipt,
        *,
        thread_id: str,
    ) -> GraphCheckpointDescriptor:
        configurable = receipt.snapshot.config.get("configurable", {})
        checkpoint_id = str(configurable.get("checkpoint_id") or "")
        checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
        descriptor = self._checkpoint_descriptor(
            receipt.snapshot,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
            exact_checkpoint_id=checkpoint_id,
        )
        if descriptor is None:
            raise RuntimeError("Graph operation checkpoint cannot be read")
        if descriptor.root_checkpoint_id != receipt.root_checkpoint_id:
            raise RuntimeError("Graph operation checkpoint changed branch root")
        return descriptor

    @staticmethod
    def _operation_root_checkpoint_id(checkpoint: Any, snapshot: Any) -> str:
        configurable = checkpoint.config.get("configurable", {})
        checkpoint_id = str(configurable.get("checkpoint_id") or "")
        checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
        if not checkpoint_id:
            raise RuntimeError("operation checkpoint has no checkpoint_id")
        if not checkpoint_ns:
            return checkpoint_id

        candidates: set[str] = set()
        parents = checkpoint.metadata.get("parents")
        if isinstance(parents, Mapping) and parents.get(""):
            candidates.add(str(parents[""]))
        checkpoint_map = snapshot.config.get("configurable", {}).get("checkpoint_map", {})
        if isinstance(checkpoint_map, Mapping) and checkpoint_map.get(""):
            candidates.add(str(checkpoint_map[""]))
        if len(candidates) != 1:
            raise RuntimeError("operation checkpoint has ambiguous branch root lineage")
        return candidates.pop()

    @classmethod
    def _latest_operation_checkpoint(cls, checkpoints: list[Any]) -> Any:
        """Select the newest receipt without relying on namespace ordering."""

        ranked = [(cls._operation_checkpoint_rank(item), item) for item in checkpoints]
        latest_rank = max(rank for rank, _ in ranked)
        latest = [item for rank, item in ranked if rank == latest_rank]
        identities = {
            (
                str(item.config.get("configurable", {}).get("checkpoint_ns") or ""),
                str(item.config.get("configurable", {}).get("checkpoint_id") or ""),
            )
            for item in latest
        }
        if len(identities) != 1:
            raise RuntimeError("durable Graph operation receipt is ambiguous")
        return latest[0]

    @staticmethod
    def _operation_checkpoint_rank(checkpoint: Any) -> tuple[datetime, int]:
        timestamp = checkpoint.checkpoint.get("ts")
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise RuntimeError("operation checkpoint has no timestamp")
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError("operation checkpoint has an invalid timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        step = checkpoint.metadata.get("step")
        if not isinstance(step, int):
            raise RuntimeError("operation checkpoint has no integer step")
        return parsed.astimezone(timezone.utc), step

    async def _reconcile_operation_receipt(
        self,
        receipt: _OperationReceipt,
        *,
        grant: RunnerCheckpointGrant,
        thread_id: str,
        metadata: Mapping[str, str],
    ) -> GraphExecutionResult:
        """Continue a partially checkpointed operation or return its boundary."""

        current_receipt = receipt
        current = await self._operation_result(current_receipt, thread_id=thread_id)
        while current.checkpoint.next_nodes and not current.interrupts:
            source = self._operation_command_config(
                current_receipt.snapshot, metadata=metadata
            )
            if source["configurable"].get("checkpoint_ns"):
                config = await self._live_nested_continuation_config(
                    current_receipt, thread_id=thread_id, metadata=metadata
                )
            else:
                config = source
            await self._invoke(grant, None, config)
            refreshed_receipt = await self._operation_receipt(
                thread_id=thread_id,
                metadata=metadata,
            )
            if refreshed_receipt is None:
                raise RuntimeError("continued operation checkpoint cannot be read")
            refreshed = await self._operation_result(
                refreshed_receipt,
                thread_id=thread_id,
            )
            previous_cursor = (
                current.checkpoint.checkpoint_ns,
                current.checkpoint.checkpoint_id,
            )
            refreshed_cursor = (
                refreshed.checkpoint.checkpoint_ns,
                refreshed.checkpoint.checkpoint_id,
            )
            if refreshed_cursor == previous_cursor:
                raise RuntimeError("LangGraph operation made no durable progress")
            current_receipt = refreshed_receipt
            current = refreshed
        return current

    async def _live_nested_continuation_config(
        self,
        receipt: _OperationReceipt,
        *,
        thread_id: str,
        metadata: Mapping[str, str],
    ) -> dict[str, Any]:
        """Continue the exact pending child without time-traveling its parent.

        An explicit root checkpoint_id makes LangGraph fork the parent and
        schedule a new child. Verify that both current saved heads still equal
        this receipt, then let the compiled root continue its pending task.
        A parent's cached pause is harmless only when the exact child resume
        source records its consumption and the child has durably advanced.
        The fenced saver checks the claim again on each resulting write.
        """

        leaf = receipt.snapshot.config.get("configurable", {})
        leaf_ns = str(leaf.get("checkpoint_ns") or "")
        leaf_id = str(leaf.get("checkpoint_id") or "")
        leaf_map = leaf.get("checkpoint_map")
        root_id = receipt.root_checkpoint_id
        if (
            not leaf_ns
            or not leaf_id
            or not isinstance(leaf_map, Mapping)
            or leaf_map.get("") != root_id
            or leaf_map.get(leaf_ns) != leaf_id
        ):
            raise RuntimeError("pending child receipt has no exact root and child map")
        root = await self._graph.aget_state(self._config(thread_id))
        root_config = root.config.get("configurable", {}) if root.config else {}
        if (
            root.created_at is None
            or root_config.get("checkpoint_ns", "") != ""
            or root_config.get("checkpoint_id") != root_id
            or not root.next
        ):
            raise RuntimeError("pending child root changed before continuation")
        matching_tasks = [
            task for task in root.tasks
            if isinstance(task.state, Mapping)
            and isinstance(task.state.get("configurable"), Mapping)
            and task.state["configurable"].get("checkpoint_ns") == leaf_ns
            and task.name in root.next
        ]
        if len(matching_tasks) != 1:
            raise RuntimeError("pending root no longer owns the exact child task")
        latest_child = await self._saver.aget_tuple(
            self._config(thread_id, checkpoint_ns=leaf_ns)
        )
        if latest_child is None:
            raise RuntimeError("pending child checkpoint is missing")
        child_config = latest_child.config.get("configurable", {})
        child_metadata = latest_child.metadata
        if (
            child_config.get("checkpoint_ns") != leaf_ns
            or child_config.get("checkpoint_id") != leaf_id
            or not isinstance(child_metadata, Mapping)
            or any(child_metadata.get(key) != value for key, value in metadata.items())
        ):
            raise RuntimeError("pending child changed before continuation")
        if root.interrupts:
            pending_task = matching_tasks[0]
            if (
                receipt.snapshot.interrupts
                or any(channel == "__interrupt__" and value for _task_id, channel, value in latest_child.pending_writes)
                or root.interrupts != pending_task.interrupts
                or any(task.interrupts for task in root.tasks if task is not pending_task)
            ):
                raise RuntimeError("pending parent interrupt is still active")
            source_id = str(metadata.get(_SOURCE_CHECKPOINT_ID_METADATA) or "")
            if not source_id or source_id == leaf_id:
                raise RuntimeError("pending parent interrupt has no advanced resume source")
            source = await self._saver.aget_tuple(self._config(
                thread_id, checkpoint_id=source_id, checkpoint_ns=leaf_ns,
            ))
            if source is None:
                raise RuntimeError("pending parent interrupt resume source is missing")
            source_config = source.config.get("configurable", {})
            source_parents = source.metadata.get("parents")
            if (
                source_config.get("checkpoint_ns") != leaf_ns
                or source_config.get("checkpoint_id") != source_id
                or not isinstance(source_parents, Mapping)
                or source_parents.get("") != root_id
                or self._operation_checkpoint_rank(latest_child) <= self._operation_checkpoint_rank(source)
                or latest_child.metadata["step"] <= source.metadata["step"]
            ):
                raise RuntimeError("pending parent interrupt resume source changed")
            resumed_tasks = {
                task_id for task_id, channel, value in source.pending_writes
                if channel == "__resume__" and value
            }
            consumed_interrupts = {
                item.id: item
                for task_id, channel, value in source.pending_writes
                if channel == "__interrupt__" and task_id in resumed_tasks
                for item in value
            }
            if consumed_interrupts != {item.id: item for item in root.interrupts}:
                raise RuntimeError("pending parent interrupt has not been consumed by this child")
        return self._config(thread_id, metadata=metadata)

    def _require_grant(
        self,
        grant: RunnerCheckpointGrant,
        *,
        thread_id: str,
        target_run_id: str,
    ) -> None:
        if self._fenced_saver is None:
            raise RuntimeError("durable Graph execution requires a fenced saver")
        if grant.thread_id != thread_id or grant.scope.run_id != target_run_id:
            raise RuntimeError("Graph execution is outside the claimed Run")

    async def _invoke(
        self,
        grant: RunnerCheckpointGrant,
        value: Any,
        config: dict[str, Any],
    ) -> None:
        if self._fenced_saver is None:
            raise RuntimeError("durable Graph execution requires a fenced saver")
        self._execution_started = True
        with self._fenced_saver.bind_execution(grant):
            if self._callbacks:
                config = {**config, "callbacks": list(self._callbacks), "metadata": {
                    **config.get("metadata", {}), "run_id": grant.scope.run_id,
                    "task_id": grant.scope.identity.task_id, "thread_id": grant.thread_id,
                }}
            await self._graph.ainvoke(
                value,
                config,
                context=grant.graph_context,
                durability="sync",
            )

    async def _update_state(
        self,
        grant: RunnerCheckpointGrant,
        config: dict[str, Any],
        values: Any,
        *,
        as_node: str | None = None,
    ) -> dict[str, Any]:
        if self._fenced_saver is None:
            raise RuntimeError("durable Graph execution requires a fenced saver")
        self._execution_started = True
        with self._fenced_saver.bind_execution(grant):
            return await self._graph.aupdate_state(config, values, as_node=as_node)

    async def _operation_result(
        self,
        receipt: _OperationReceipt,
        *,
        thread_id: str,
    ) -> GraphExecutionResult:
        root = await self._graph.aget_state(
            self._config(
                thread_id,
                checkpoint_id=receipt.root_checkpoint_id,
                checkpoint_ns="",
            )
        )
        root_descriptor = self._checkpoint_descriptor(
            root,
            thread_id=thread_id,
            checkpoint_ns="",
            exact_checkpoint_id=receipt.root_checkpoint_id,
        )
        if root_descriptor is None:
            raise RuntimeError("Graph operation root checkpoint cannot be read")
        leaf_descriptor = self._operation_checkpoint_descriptor(
            receipt,
            thread_id=thread_id,
        )
        projected = project_checkpoint_cursor(
            root,
            receipt.snapshot,
            thread_id=thread_id,
        )
        projected_cursor = (
            projected.checkpoint.thread_id,
            projected.checkpoint.checkpoint_ns,
            projected.checkpoint.checkpoint_id,
            projected.checkpoint.root_checkpoint_id,
            projected.checkpoint.checkpoint_map_digest,
        )
        exact_cursor = (
            leaf_descriptor.thread_id,
            leaf_descriptor.checkpoint_ns,
            leaf_descriptor.checkpoint_id,
            leaf_descriptor.root_checkpoint_id,
            leaf_descriptor.checkpoint_map_digest,
        )
        if projected_cursor != exact_cursor:
            raise RuntimeError("Graph operation cursor projection changed exact lineage")
        return projected.model_copy(update={"checkpoint": leaf_descriptor})

    def _checkpoint_descriptor(
        self,
        snapshot: Any,
        *,
        thread_id: str,
        checkpoint_ns: str,
        exact_checkpoint_id: str | None,
    ) -> GraphCheckpointDescriptor | None:
        if not snapshot.config:
            return None
        configurable = snapshot.config.get("configurable", {})
        persisted_id = configurable.get("checkpoint_id")
        if not persisted_id:
            return None
        persisted_thread = str(configurable.get("thread_id", ""))
        persisted_ns = str(configurable.get("checkpoint_ns", ""))
        if persisted_thread != thread_id or persisted_ns != checkpoint_ns:
            raise RuntimeError("LangGraph returned a checkpoint outside the requested scope")
        if exact_checkpoint_id is not None and str(persisted_id) != exact_checkpoint_id:
            raise RuntimeError("LangGraph returned a different checkpoint than requested")
        lineage = describe_checkpoint_lineage(configurable)
        return GraphCheckpointDescriptor(
            thread_id=thread_id,
            checkpoint_id=str(persisted_id),
            checkpoint_ns=persisted_ns,
            root_checkpoint_id=lineage.root_checkpoint_id,
            checkpoint_map=lineage.checkpoint_map,
            checkpoint_map_digest=lineage.checkpoint_map_digest,
            next_nodes=tuple(snapshot.next or ()),
            created_at=snapshot.created_at,
        )

    @staticmethod
    def _metadata_text(metadata: Mapping[str, Any], key: str) -> str | None:
        value = metadata.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Graph runtime is closed")


__all__ = ["AsyncPostgresGraphRuntime", "GraphBuilder"]
