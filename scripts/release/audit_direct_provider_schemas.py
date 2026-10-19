"""Measure registered and final FastMCP declarations without opening a runtime.

Application mocks are registration placeholders only: no tools are invoked and
no MCP transport, Session, database, Storage or Worker is created. Token counts
are explicitly local o200k_base proxies, never model-input or billed usage.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import tiktoken

from CAD2BIM_mcp.direct import server as direct_server


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write(path: Path, value: object) -> None:
    path.write_text(_json(value), encoding="utf-8")


def _measure(value: object, encoding, *, text: bool = False) -> dict[str, object]:
    serialized = value if text else _json(value)
    encoded = serialized.encode("utf-8")
    return {
        "utf8_bytes": len(encoded),
        "proxy_tokens": len(encoding.encode(serialized, disallowed_special=())),
    }


async def audit(destination: Path, baseline: Path | None) -> dict[str, object]:
    destination.mkdir(parents=True, exist_ok=False)
    server = direct_server.create_direct_mcp(Mock())
    final_tools = await server.list_tools()
    registered_tools = [await server.get_tool(tool.name) for tool in final_tools]

    def declarations(tools):
        return [
            tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in tools
        ]

    registered, final = declarations(registered_tools), declarations(final_tools)
    _write(destination / "registered.json", registered)
    _write(destination / "declarations.json", final)
    encoding = tiktoken.get_encoding("o200k_base")
    rows = []
    for raw, tool in zip(registered, final, strict=True):
        rows.append({
            "tool": tool["name"],
            "description": _measure(tool.get("description", ""), encoding, text=True),
            "input": _measure(tool["inputSchema"], encoding),
            "output": _measure(tool.get("outputSchema"), encoding),
            "full": _measure(tool, encoding),
            "registered_output": _measure(raw.get("outputSchema"), encoding),
        })
    report = {
        "method": "local static registration; final server.list_tools() including middleware",
        "encoding": "o200k_base",
        "counts_are": "proxy tokens; actual native model input and billed usage unmeasured",
        "cwd": str(Path.cwd()),
        "python": sys.executable,
        "source": str(Path(direct_server.__file__).resolve()),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], text=True),
        "versions": {name: importlib.metadata.version(name) for name in ("pydantic", "fastmcp", "mcp", "tiktoken")},
        "tool_count": len(final),
        "full_declarations": _measure(final, encoding),
        "tools": rows,
    }
    if baseline is not None:
        original = json.loads(baseline.read_text(encoding="utf-8"))
        before = {tool["name"]: tool for tool in original}
        if list(before) != [tool["name"] for tool in final]:
            raise ValueError("Tool set/order changed; compare matching frozen surfaces separately")
        comparisons = []
        for tool in final:
            old = before[tool["name"]]
            comparisons.append({
                "tool": tool["name"],
                "description_equal": old.get("description") == tool.get("description"),
                "input_equal": old["inputSchema"] == tool["inputSchema"],
                "output_equal": old.get("outputSchema") == tool.get("outputSchema"),
                "before": {
                    "description": _measure(old.get("description", ""), encoding, text=True),
                    "input": _measure(old["inputSchema"], encoding),
                    "output": _measure(old.get("outputSchema"), encoding),
                    "full": _measure(old, encoding),
                },
            })
        report["baseline"] = {"path": str(baseline.resolve()), "full_declarations": _measure(original, encoding)}
        report["comparison"] = comparisons
    _write(destination / "metrics.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="A new evidence directory; existing runs are never overwritten")
    parser.add_argument("--baseline", type=Path, help="Prior declarations.json for D/I/O comparison")
    args = parser.parse_args()
    report = asyncio.run(audit(args.output_dir, args.baseline))
    print(_json({key: report[key] for key in ("tool_count", "full_declarations", "counts_are")}))


if __name__ == "__main__":
    main()
