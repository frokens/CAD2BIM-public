#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import site
import sys
import sysconfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register project src path into current interpreter via .pth file."
    )
    parser.add_argument(
        "--project-root",
        required=True,
        help="Absolute path to project root.",
    )
    parser.add_argument(
        "--src-dir",
        default="src",
        help="Relative src directory under project root (default: src).",
    )
    parser.add_argument(
        "--pth-name",
        default="cad2bim_local_src.pth",
        help="Name of the .pth file to write in site-packages.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    project_root = pathlib.Path(args.project_root).resolve()
    src_path = (project_root / args.src_dir).resolve()

    if not project_root.exists():
        print(f"[ERROR] project root not found: {project_root}", file=sys.stderr)
        return 1

    if not src_path.is_dir():
        print(f"[ERROR] src path not found: {src_path}", file=sys.stderr)
        return 1

    desired = f"{src_path}\n"

    # Write to multiple site-packages candidates for portability across Python/OS layouts.
    candidates: list[pathlib.Path] = []
    for key in ("purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            path = pathlib.Path(value)
            if path not in candidates:
                candidates.append(path)

    try:
        for value in site.getsitepackages():
            path = pathlib.Path(value)
            if path not in candidates:
                candidates.append(path)
    except Exception:
        pass

    if not candidates:
        print("[ERROR] cannot resolve site-packages paths", file=sys.stderr)
        return 1

    wrote_count = 0
    for site_packages in candidates:
        site_packages.mkdir(parents=True, exist_ok=True)
        pth_file = site_packages / args.pth_name

        previous = None
        if pth_file.exists():
            previous = pth_file.read_text(encoding="utf-8")

        pth_file.write_text(desired, encoding="utf-8")

        if previous is None:
            action = "Created"
        elif previous == desired:
            action = "Verified"
        else:
            action = "Updated"

        print(f"{action}: {pth_file}")
        wrote_count += 1

    print(f"Updated site-packages count: {wrote_count}")
    print(f"Registered src path: {src_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
