from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from CAD2BIM_backend.infrastructure.postgres.models import DirectSessionBindingModel
from tests.process_support.ifc_golden_seed.materializer import (
    MaterializationError,
    materialize_seed,
)
from tests.process_support.ifc_golden_seed.test_support import (
    IsolatedDatabaseUnavailable,
    create_seed_runtime,
    export_test_seed,
    isolated_database_pair,
    open_seed_scope,
    provision_seed_runtime_roles,
    runtime_database_url,
)


pytestmark = [pytest.mark.integration, pytest.mark.network]


@pytest.mark.asyncio
async def test_restore_rejects_a_nonempty_database_before_creating_target_identity(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("CAD2BIM_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("CAD2BIM_TEST_DATABASE_URL is not configured")
    provision_seed_runtime_roles(database_url)
    source = create_seed_runtime(
        runtime_database_url(database_url),
        tmp_path / "source-storage",
    )
    target = None
    target_admin = None
    try:
        await source.database.verify_ready()
        seed = await export_test_seed(
            source,
            output_root=tmp_path / "source-seed",
            configured_subject=f"seed-nonempty-source-{uuid4().hex}",
            session_name="Golden Seed nonempty-target source",
        )
        try:
            with isolated_database_pair(database_url) as databases:
                target = create_seed_runtime(
                    databases.runtime_url_a,
                    tmp_path / "occupied-storage",
                )
                target_admin = create_seed_runtime(
                    databases.admin_url_a,
                    tmp_path / "admin-unused-storage",
                )
                await target.database.verify_ready()
                await target_admin.database.verify_ready()
                await open_seed_scope(
                    target,
                    configured_subject=f"seed-existing-{uuid4().hex}",
                    session_name="Existing target data",
                )

                rejected_subject = f"seed-must-not-exist-{uuid4().hex}"
                rejected_storage = tmp_path / "rejected-storage"
                with pytest.raises(
                    MaterializationError,
                    match="target database is not empty",
                ):
                    await materialize_seed(
                        seed.exported.root_path,
                        session_factory=target.database.session_factory,
                        admin_session_factory=target_admin.database.session_factory,
                        repository=target.repository,
                        identity_directory=target.identity_directory,
                        storage_root=rejected_storage,
                        configured_subject=rejected_subject,
                        session_name="Rejected restored copy",
                    )

                async with target_admin.database.session_factory() as session:
                    binding_count = await session.scalar(
                        select(func.count())
                        .select_from(DirectSessionBindingModel)
                        .where(
                            DirectSessionBindingModel.configured_subject
                            == rejected_subject
                        )
                    )
                assert binding_count == 0
                assert not rejected_storage.exists()
        except IsolatedDatabaseUnavailable as exc:
            pytest.skip(str(exc))
    finally:
        if target is not None:
            await target.close()
        if target_admin is not None:
            await target_admin.close()
        await source.close()
