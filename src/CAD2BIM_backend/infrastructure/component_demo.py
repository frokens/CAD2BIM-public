"""Local Component runtime entry over the existing backend, Runner and MCP."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import subprocess
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
import uvicorn
from openai import AsyncOpenAI
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from CAD2BIM_backend.infrastructure.agent_skills import AgentSkills
from CAD2BIM_backend.bootstrap import open_backend_runtime
from CAD2BIM_backend.config import Settings
from CAD2BIM_backend.domain.execution_protocol import ExecutionProtocolBinding
from CAD2BIM_backend.infrastructure.chatgpt_auth import ChatGPTAuth
from CAD2BIM_backend.infrastructure.chatgpt_model import create_chatgpt_plan_model, model_execution_error
from CAD2BIM_backend.infrastructure.langgraph.setup import setup_checkpoint_schema
from CAD2BIM_backend.infrastructure.mcp_domain_client import RunnerDomainMcpClient
from CAD2BIM_backend.infrastructure.mcp_state_client import RunnerStateMcpClient
from CAD2BIM_backend.infrastructure.postgres.runtime_roles import provision_runtime_login_roles
from CAD2BIM_backend.infrastructure.runner_domain_tokens import RunnerDomainTokenCodec
from CAD2BIM_backend.infrastructure.state_access_tokens import StateAccessTokenCodec
from CAD2BIM_mcp.runner.backend_client import RunnerStateBackendClient
from CAD2BIM_mcp.runner.server import create_runner_state_mcp


def _private_write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


async def prepare(directory: Path, *, database_port: int, api_port: int, mcp_port: int) -> None:
    """Prepare one isolated local database without touching existing services."""

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = directory / "runtime.json"
    if config_path.exists():
        raise ValueError("runtime already prepared; use its existing serve entry")
    configured_bin = os.environ.get("CAD2BIM_POSTGRES_BIN")
    initdb = shutil.which("initdb")
    postgres = (Path(configured_bin).expanduser() if configured_bin else
                Path(initdb).parent if initdb else None)
    if postgres is None:
        pg_config = shutil.which("pg_config")
        if pg_config:
            result = await asyncio.to_thread(subprocess.run, [pg_config, "--bindir"],
                check=True, capture_output=True, text=True)
            postgres = Path(result.stdout.strip())
    if postgres is None or not all((postgres / name).is_file() for name in ("initdb", "pg_ctl")):
        raise ValueError("Install PostgreSQL 16 and set CAD2BIM_POSTGRES_BIN to its bin directory, or put initdb/pg_ctl on PATH")
    cluster = directory / "postgres"
    if not (cluster / "PG_VERSION").exists():
        await asyncio.to_thread(subprocess.run, [str(postgres / "initdb"), "-D", str(cluster), "--auth=trust", "-U", "component_admin"], check=True, stdout=subprocess.DEVNULL)
    socket_dir = directory / "socket"
    socket_dir.mkdir(exist_ok=True)
    status = await asyncio.to_thread(subprocess.run, [str(postgres / "pg_ctl"), "-D", str(cluster), "status"], stdout=subprocess.DEVNULL)
    if status.returncode:
        await asyncio.to_thread(subprocess.run, [
            str(postgres / "pg_ctl"), "-D", str(cluster), "-l", str(directory / "postgres.log"),
            "-o", f"-h 127.0.0.1 -p {database_port} -k {socket_dir}", "start",
        ], check=True, stdout=subprocess.DEVNULL)
    admin_url = f"postgresql+psycopg://component_admin@127.0.0.1:{database_port}/component_demo"
    with psycopg.connect(f"postgresql://component_admin@127.0.0.1:{database_port}/postgres", autocommit=True) as connection:
        if connection.execute("SELECT 1 FROM pg_database WHERE datname = 'component_demo'").fetchone() is None:
            connection.execute("CREATE DATABASE component_demo")
    alembic = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    alembic.set_main_option("sqlalchemy.url", admin_url)
    command.upgrade(alembic, "head")
    await setup_checkpoint_schema(admin_url)
    passwords = {
        name: secrets.token_hex(24)
        for name in ("CAD2BIM_API_POSTGRES_PASSWORD", "CAD2BIM_RUNNER_POSTGRES_PASSWORD", "CAD2BIM_MCP_POSTGRES_PASSWORD")
    }
    provision_runtime_login_roles(admin_url, environ=passwords)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    _private_write(directory / "signing.pem", private_pem)
    _private_write(directory / "public.pem", private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    issuer, audience = "https://cad2bim.local/component", "cad2bim-component"
    now = datetime.now(UTC)
    bearer = jwt.encode({
        "iss": issuer, "aud": audience, "sub": "component-demo-user",
        "iat": int(now.timestamp()), "exp": int((now + timedelta(days=3)).timestamp()),
    }, private_pem, algorithm="RS256")
    settings = {
        "api_url": f"http://127.0.0.1:{api_port}", "mcp_url": f"http://127.0.0.1:{mcp_port}/mcp",
        "api_database_url": f"postgresql+psycopg://cad2bim_api_login:{passwords['CAD2BIM_API_POSTGRES_PASSWORD']}@127.0.0.1:{database_port}/component_demo",
        "runner_database_url": f"postgresql+psycopg://cad2bim_runner_login:{passwords['CAD2BIM_RUNNER_POSTGRES_PASSWORD']}@127.0.0.1:{database_port}/component_demo",
        "admin_database_url": admin_url,
        "issuer": issuer, "audience": audience, "state_secret": base64.b64encode(secrets.token_bytes(48)).decode(),
        "api_port": api_port, "mcp_port": mcp_port,
    }
    _private_write(config_path, json.dumps(settings, indent=2))
    _private_write(directory / "frontend.env", f"COMPONENT_BACKEND_URL={settings['api_url']}\nCOMPONENT_BACKEND_BEARER={bearer}\n")
    print(json.dumps({"runtime_dir": str(directory), "api_url": settings["api_url"], "mcp_url": settings["mcp_url"], "prepared": True}))


async def _drive_runners(runners: tuple[Any, ...], api_server: Any, mcp_server: Any) -> None:
    """Run existing executors independently; unknown execution errors remain fatal."""
    while not api_server.started or not mcp_server.started:
        if api_server.should_exit or mcp_server.should_exit:
            return
        await asyncio.sleep(0.05)

    async def drive_slot(slot: int, runner: Any) -> None:
        while not api_server.should_exit and not mcp_server.should_exit:
            try:
                await runner.execute_next()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if model_execution_error(error) is None:
                    logging.getLogger(__name__).exception(
                        "Component Runner slot %s stopped on an unknown execution failure; "
                        "stopping service with saved Run/checkpoint available for recovery", slot,
                    )
                    raise
                logging.getLogger(__name__).exception(
                    "Component Runner slot %s model generation failed; original Run is paused "
                    "and can continue from its saved checkpoint", slot,
                )
                await asyncio.sleep(1)
            await asyncio.sleep(0.25)

    tasks = [asyncio.create_task(drive_slot(slot, runner), name=f"component-runner-slot-{slot}")
             for slot, runner in enumerate(runners, start=1)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _serve_runtime(backend: Any, api_server: Any, mcp_server: Any) -> None:
    """Settle all executor tasks before the caller closes shared runtime resources."""
    servers = [asyncio.create_task(api_server.serve()), asyncio.create_task(mcp_server.serve())]
    runners = asyncio.create_task(_drive_runners(backend.runners, api_server, mcp_server))
    try:
        done, _ = await asyncio.wait([*servers, runners], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        api_server.should_exit = mcp_server.should_exit = True
        runners.cancel()
        await asyncio.gather(runners, return_exceptions=True)
        # Uvicorn's normal five-second shutdown remains available. A server that
        # cannot settle must not keep the model tasks or shared pools alive.
        _, pending = await asyncio.wait(servers, timeout=6)
        for task in pending:
            task.cancel()
        await asyncio.gather(*servers, return_exceptions=True)


async def serve(
    directory: Path, *, model_name: str | None = None, service_tier: str | None = None,
    runner_slots: int | None = None, reasoning_effort: str | None = None,
) -> None:
    config: dict[str, Any] = json.loads((directory / "runtime.json").read_text())
    model_name = model_name or config.get("model", "gpt-6.1-sol")
    service_tier = service_tier or config.get("service_tier", "default")
    reasoning_effort = reasoning_effort or config.get("reasoning_effort", "max")
    if reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported reasoning_effort")
    runner_slots = config.get("runner_slots", 2) if runner_slots is None else runner_slots
    if isinstance(runner_slots, bool) or not isinstance(runner_slots, int) or not 1 <= runner_slots <= 4:
        raise ValueError("runner_slots must be an integer between 1 and 4")
    secret = base64.b64decode(config["state_secret"], validate=True)
    repository_root = Path(__file__).resolve().parents[3]
    skills = AgentSkills(repository_root / "skills")
    await skills.discover()
    reference_directory = repository_root / "docs/work/i0/component-demo-tool-handoff-20261003/runner-input"
    initial_reference_text = "\n\n".join(
        f"--- {name} ---\n{(reference_directory / name).read_text(encoding='utf-8')}"
        for name in (
            "injection-guidance.md", "demo-guidance.md", "geometry-writes.json", "family-write.json",
            "preview-cases.json", "observations.json", "publish.json",
        )
    )
    state_codec, domain_codec = StateAccessTokenCodec(secret), RunnerDomainTokenCodec(secret)
    mcp = create_runner_state_mcp(
        codec=state_codec, backend=RunnerStateBackendClient(config["api_url"]),
        domain_codec=domain_codec,
    )
    holder: dict[str, Any] = {}
    requests_dir = directory / "model-requests"
    requests_dir.mkdir(exist_ok=True, mode=0o700)
    request_counter = max(
        (int(path.name.partition("-")[0]) for path in requests_dir.glob("*-request.json")
         if path.name.partition("-")[0].isdecimal()),
        default=0,
    )

    async def record_request(request: httpx.Request) -> None:
        nonlocal request_counter
        request_counter += 1
        _private_write(requests_dir / f"{request_counter:04d}-request.json", request.content)

    async with AsyncExitStack() as stack:
        auth = await stack.enter_async_context(ChatGPTAuth())
        model_http = await stack.enter_async_context(httpx.AsyncClient(event_hooks={"request": [record_request]}, timeout=300))
        settings = Settings.load()
        input_token_client = None
        if settings.openai_api_key:
            input_token_client = await stack.enter_async_context(AsyncOpenAI(
                api_key=settings.openai_api_key, max_retries=0, timeout=10,
            ))
        model = create_chatgpt_plan_model(
            auth.get_access_token, http_async_client=model_http,
            model_name=model_name, service_tier=service_tier, reasoning_effort=reasoning_effort,
            input_token_client=input_token_client,
        )

        async def tools_factory(artifacts, _feedback_nodes):
            from CAD2BIM_backend.infrastructure.component_tools import assemble_component_tools

            return await assemble_component_tools(
                mcp, artifacts=artifacts, skills=skills,
                domain_client=RunnerDomainMcpClient(mcp_url=config["mcp_url"], codec=domain_codec),
                state_client=RunnerStateMcpClient(mcp_url=config["mcp_url"], codec=state_codec),
                grant_provider=lambda _config, _runtime: holder["backend"].graph_runtime.require_current_execution_grant(),
                run_provider=lambda _config, runtime: holder["backend"].repository.get_run(runtime.context.scope.owner_id, runtime.context.scope.run_id),
                graph_runtime_provider=lambda _config, _runtime: holder["backend"].graph_runtime,
            )

        backend = await open_backend_runtime(
            api_settings=Settings(database_url=config["api_database_url"]),
            runner_settings=Settings(database_url=config["runner_database_url"]),
            public_key_pem=(directory / "public.pem").read_bytes(), issuer=config["issuer"], audience=config["audience"],
            execution_protocol=ExecutionProtocolBinding(bundle_id="component", module="component", stage_key="component"),
            runner_id=f"component-runner-{os.getpid()}", state_access_secret=secret,
            artifact_storage_root=directory / "storage", ifc_storage_root=directory / "storage",
            component_model=model,
            recoverable_execution_error=model_execution_error,
            runner_slots=runner_slots,
            component_tools_factory=tools_factory, component_skills=skills,
            component_initial_reference_text=initial_reference_text,
            component_input_limit=config.get("input_limit", 256_000),
            component_output_reserve=config.get("output_reserve", 8_192),
            component_summary_input_limit=config.get("summary_input_limit", 256_000),
        )
        holder["backend"] = backend
        api_server = uvicorn.Server(uvicorn.Config(
            backend.api, host="127.0.0.1", port=config["api_port"], log_level="warning",
            timeout_graceful_shutdown=5,
        ))
        mcp_server = uvicorn.Server(uvicorn.Config(
            mcp.http_app(path="/mcp"), host="127.0.0.1", port=config["mcp_port"], log_level="warning",
            timeout_graceful_shutdown=5,
        ))

        try:
            print(json.dumps({
                "api_url": config["api_url"], "mcp_url": config["mcp_url"], "runner": "starting",
                "model": model_name, "requested_service_tier": service_tier,
                "reasoning_effort": reasoning_effort,
                "runner_slots": len(backend.runners),
            }), flush=True)
            await _serve_runtime(backend, api_server, mcp_server)
        finally:
            await backend.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Local CAD2BIM Component backend")
    parser.add_argument("command", choices=("prepare", "serve"))
    parser.add_argument("--runtime-dir", type=Path, default=Path(".test/component-runtime"))
    parser.add_argument("--database-port", type=int, default=55883)
    parser.add_argument("--api-port", type=int, default=8763)
    parser.add_argument("--mcp-port", type=int, default=8764)
    parser.add_argument("--model", choices=("gpt-6.1-sol", "gpt-6-astra"))
    parser.add_argument("--service-tier", choices=("default", "fast", "priority"))
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--runner-slots", type=int, choices=range(1, 5))
    args = parser.parse_args()
    directory = args.runtime_dir.resolve()
    if args.command == "prepare":
        asyncio.run(prepare(directory, database_port=args.database_port, api_port=args.api_port, mcp_port=args.mcp_port))
    else:
        asyncio.run(serve(directory, model_name=args.model, service_tier=args.service_tier,
                          runner_slots=args.runner_slots, reasoning_effort=args.reasoning_effort))


if __name__ == "__main__":
    cli()
