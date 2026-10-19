"""Checkpoint saver decorator that observes only successfully committed state."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from CAD2BIM_backend.infrastructure.langgraph.checkpoint_lineage import (
    describe_checkpoint_lineage,
)
from CAD2BIM_backend.ports.checkpoint_observers import (
    DurableCheckpointCommit,
    DurableCheckpointObserver,
)


_OPERATION_ID_METADATA = "cad2bim_resume_operation_id"
_TARGET_RUN_ID_METADATA = "cad2bim_target_run_id"


class ObservingAsyncCheckpointSaver(BaseCheckpointSaver[str]):
    """Delegate storage first, then notify; historical reads retry notification."""

    def __init__(
        self,
        delegate: BaseCheckpointSaver[str],
        observer: DurableCheckpointObserver | None = None,
    ) -> None:
        super().__init__()
        self.serde = delegate.serde
        self._delegate = delegate
        self._observer = observer
        self._observe_reads: ContextVar[bool] = ContextVar("checkpoint_observe_reads", default=True)

    @contextmanager
    def without_read_observation(self):
        """Read an existing display source without replaying commit notifications."""
        token = self._observe_reads.set(False)
        try:
            yield
        finally:
            self._observe_reads.reset(token)

    def bind_observer(self, observer: DurableCheckpointObserver) -> None:
        """Attach the composition-root observer once, before execution."""

        if self._observer is not None:
            raise RuntimeError("checkpoint observer is already bound")
        self._observer = observer

    @property
    def config_specs(self) -> list:
        return self._delegate.config_specs

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self._delegate.get_next_version(current, channel)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        persisted = await self._delegate.aput(config, checkpoint, metadata, new_versions)
        observation_config = dict(persisted)
        if config.get("metadata") is not None:
            observation_config["metadata"] = config["metadata"]
        await self._observe(
            observation_config,
            checkpoint,
            metadata,
            parent_config=config,
        )
        return persisted

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await self._delegate.aput_writes(config, writes, task_id, task_path)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        item = await self._delegate.aget_tuple(config)
        if item is not None and self._observe_reads.get():
            await self._observe_tuple(item)
        return item

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async for item in self._delegate.alist(
            config,
            filter=filter,
            before=before,
            limit=limit,
        ):
            if self._observe_reads.get():
                await self._observe_tuple(item)
            yield item

    async def adelete_thread(self, thread_id: str) -> None:
        await self._delegate.adelete_thread(thread_id)

    async def _observe_tuple(self, item: CheckpointTuple) -> None:
        await self._observe(
            item.config,
            item.checkpoint,
            item.metadata,
            parent_config=item.parent_config,
        )

    async def _observe(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        *,
        parent_config: RunnableConfig | None,
    ) -> None:
        if self._observer is None:
            return
        target_run_id = _metadata_text(config, metadata, _TARGET_RUN_ID_METADATA)
        if not target_run_id or target_run_id == "__volatile__":
            return
        configurable = config.get("configurable") or {}
        if not isinstance(configurable, Mapping):
            raise RuntimeError("committed checkpoint has no configurable identity")
        thread_id = str(configurable.get("thread_id") or "")
        checkpoint_id = str(configurable.get("checkpoint_id") or checkpoint.get("id") or "")
        checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
        if checkpoint_id and configurable.get("checkpoint_id") != checkpoint_id:
            configurable = {**configurable, "checkpoint_id": checkpoint_id}
        configurable = _with_persisted_checkpoint_map(
            configurable,
            metadata,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=checkpoint_ns,
        )
        lineage = describe_checkpoint_lineage(configurable)
        values = checkpoint.get("channel_values")
        if not isinstance(values, Mapping):
            raise RuntimeError("committed checkpoint channel_values must be an object")
        parent_id = _parent_checkpoint_id(parent_config)
        await self._observer.checkpoint_committed(
            DurableCheckpointCommit(
                target_run_id=target_run_id,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
                checkpoint_ns=checkpoint_ns,
                root_checkpoint_id=lineage.root_checkpoint_id,
                checkpoint_map=lineage.checkpoint_map,
                checkpoint_map_digest=lineage.checkpoint_map_digest,
                parent_checkpoint_id=parent_id,
                source=_optional_text(metadata.get("source")),
                step=_optional_int(metadata.get("step")),
                operation_id=_metadata_text(config, metadata, _OPERATION_ID_METADATA),
                created_at=_optional_text(checkpoint.get("ts")),
                values=dict(values),
                updated_channels=tuple(checkpoint.get("updated_channels") or ()),
            )
        )


def _metadata_text(
    config: RunnableConfig,
    metadata: Mapping[str, Any],
    key: str,
) -> str | None:
    configured = config.get("metadata") or {}
    value = configured.get(key) if isinstance(configured, Mapping) else None
    if value is None:
        value = metadata.get(key)
    return _optional_text(value)


def _parent_checkpoint_id(config: RunnableConfig | None) -> str | None:
    if config is None:
        return None
    configurable = config.get("configurable") or {}
    if not isinstance(configurable, Mapping):
        return None
    return _optional_text(configurable.get("checkpoint_id"))


def _with_persisted_checkpoint_map(
    configurable: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    checkpoint_id: str,
    checkpoint_ns: str,
) -> dict[str, Any]:
    """Rebuild the exact branch map saved by LangGraph in metadata.parents."""

    result = dict(configurable)
    checkpoint_map: dict[str, str] = {}
    for source in (metadata.get("parents"), configurable.get("checkpoint_map")):
        if source is None:
            continue
        if not isinstance(source, Mapping):
            raise RuntimeError("committed checkpoint branch map must be an object")
        for raw_namespace, raw_checkpoint_id in source.items():
            namespace = str(raw_namespace)
            parent_id = str(raw_checkpoint_id or "").strip()
            if not parent_id:
                continue
            current = checkpoint_map.get(namespace)
            if current is not None and current != parent_id:
                raise RuntimeError("committed checkpoint branch map is inconsistent")
            checkpoint_map[namespace] = parent_id
    # LangGraph writes ``metadata.parents`` before committing the new
    # checkpoint.  Its entry for the active namespace therefore identifies the
    # previous checkpoint on that branch, not the checkpoint just persisted.
    # Keep all ancestor identities but advance the active namespace (including
    # the root namespace ``""``) to the committed checkpoint.
    checkpoint_map[checkpoint_ns] = checkpoint_id
    result["checkpoint_map"] = checkpoint_map
    return result


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


__all__ = ["ObservingAsyncCheckpointSaver"]
