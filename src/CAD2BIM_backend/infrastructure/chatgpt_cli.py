"""Local SIWC authorization and bounded technical model verification entry."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from CAD2BIM_backend.infrastructure.chatgpt_auth import ChatGPTAuth
from CAD2BIM_backend.infrastructure.chatgpt_model import create_chatgpt_plan_model


def _save(path: Path, content: str | bytes) -> None:
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    path.chmod(0o600)


async def _verify(
    auth: ChatGPTAuth, evidence_root: Path, *, model_name: str = "gpt-6.1-sol",
    service_tier: str = "default",
) -> dict:
    """One text+image+function call, with no domain execution or follow-up inference."""
    from PIL import Image, ImageDraw, ImageFont

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = evidence_root / f"{stamp}-{uuid.uuid4().hex[:6]}"
    directory.mkdir(mode=0o700, parents=True)
    image = Image.new("RGB", (480, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((35, 35, 145, 145), fill=(0, 160, 0))
    draw.rectangle((260, 50, 430, 150), fill=(0, 80, 220))
    draw.text((195, 180), "F7", fill="black", font=ImageFont.load_default(size=36))
    image.save(directory / "input.png")
    image_url = "data:image/png;base64," + base64.b64encode((directory / "input.png").read_bytes()).decode()

    async def record(response: httpx.Response) -> None:
        # A verification projection only: never save Authorization or other headers.
        _save(directory / "request.json", response.request.content)
        _save(directory / "response.sse", await response.aread())
        _save(directory / "http.json", json.dumps({
            "status": response.status_code, "request_id": response.headers.get("x-request-id"),
        }, indent=2))

    tool = {
        "type": "function", "function": {
            "name": "record_image_observation", "description": "Record what is visibly present in the supplied image. This is a technical protocol check.",
            "parameters": {
                "type": "object", "properties": {
                    key: {"type": "string"} for key in (
                        "left_shape", "left_color", "right_shape", "right_color", "label",
                    )
                }, "required": ["left_shape", "left_color", "right_shape", "right_color", "label"],
                "additionalProperties": False,
            }, "strict": True,
        },
    }
    async with httpx.AsyncClient(event_hooks={"response": [record]}, timeout=300) as client:
        model = create_chatgpt_plan_model(
            auth.get_access_token, http_async_client=client,
            model_name=model_name, service_tier=service_tier,
        )
        message = await model.bind_tools([tool], tool_choice="required").ainvoke([
            SystemMessage("Report the image using the supplied tool. Use English lower-case shape and color names. Copy the visible label exactly."),
            HumanMessage(content=[
                {"type": "text", "text": "Identify the left and right shapes, their colors, and the label in this image."},
                {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
            ]),
        ])
    _save(directory / "ai_message.json", message.model_dump_json(indent=2))
    expected = {"left_shape": "circle", "left_color": "green", "right_shape": "rectangle", "right_color": "blue", "label": "F7"}
    actual = message.tool_calls[0]["args"] if len(message.tool_calls) == 1 else None
    result = {
        "evidence_dir": str(directory.resolve()), "status": message.response_metadata.get("status"),
        "model": message.response_metadata.get("model"),
        "requested_model": model_name,
        "requested_service_tier": message.response_metadata.get("requested_service_tier"),
        "service_tier": message.response_metadata.get("service_tier"),
        "fast_applied": message.response_metadata.get("service_tier") in {"priority", "fast"},
        "reasoning": message.response_metadata.get("reasoning"),
        "image_observation_matches": actual == expected, "tool_calls": message.tool_calls,
        "encrypted_reasoning_retained": any(isinstance(block, dict) and bool(block.get("encrypted_content")) for block in message.content),
        "usage": message.usage_metadata,
    }
    _save(directory / "result.json", json.dumps(result, indent=2))
    return result


async def _run(args: argparse.Namespace) -> dict:
    async with ChatGPTAuth(args.storage_dir) as auth:
        if args.command == "sign-in":
            return await auth.sign_in()
        if args.command == "status":
            return await auth.get_session()
        return await _verify(auth, args.evidence_dir, model_name=args.model, service_tier=args.service_tier)


def cli() -> None:
    parser = argparse.ArgumentParser(description="CAD2BIM Component Demo ChatGPT connection")
    parser.add_argument("command", choices=("status", "sign-in", "verify"))
    parser.add_argument("--storage-dir", type=Path)
    parser.add_argument("--evidence-dir", type=Path, default=Path("artifacts/component-model"))
    parser.add_argument("--model", choices=("gpt-6.1-sol", "gpt-6-astra"), default="gpt-6.1-sol")
    parser.add_argument("--service-tier", choices=("default", "fast", "priority"), default="default")
    result = asyncio.run(_run(parser.parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
