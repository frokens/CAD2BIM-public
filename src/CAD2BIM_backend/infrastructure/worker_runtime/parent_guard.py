"""Standard-library launcher: stop a worker when its owning parent pipe closes.

This process is outside the worker sandbox. It starts only the already selected
command, shares its process group, and never reads domain input or output.
"""

from __future__ import annotations

import argparse
import os
import selectors
import signal
import subprocess
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-fd", type=int, required=True)
    parser.add_argument("--pid-fd", type=int, required=True)
    parser.add_argument("--start-fd", type=int, required=True)
    parser.add_argument("--pass-fds", default="")
    parser.add_argument("--capacity-fds", default="")
    parser.add_argument("--cleanup-seconds", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    payload_fds = tuple(int(value) for value in args.pass_fds.split(",") if value)
    capacity_fds = tuple(int(value) for value in args.capacity_fds.split(",") if value)
    # The child cannot exist until its owner has durably registered this group.
    os.write(args.pid_fd, b"READY\n")
    with selectors.DefaultSelector() as gate:
        gate.register(args.parent_fd, selectors.EVENT_READ)
        gate.register(args.start_fd, selectors.EVENT_READ)
        while True:
            events = gate.select()
            if any(key.fd == args.parent_fd for key, _ in events):
                # Parent liveness is a write-only pipe; any readable event is
                # EOF or a protocol violation and must close the start gate.
                return 1
            if any(key.fd == args.start_fd for key, _ in events):
                if os.read(args.start_fd, 1) != b"1":
                    return 1
                if any(key.fd == args.parent_fd for key, _ in gate.select(timeout=0)):
                    return 1
                break
    os.close(args.start_fd)
    # Group termination must reach the child while this guardian waits to reap it.
    process = subprocess.Popen(command, close_fds=True, pass_fds=(*payload_fds, *capacity_fds))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for descriptor in payload_fds:
        os.close(descriptor)
    os.write(args.pid_fd, f"{process.pid}\n".encode("ascii"))
    os.close(args.pid_fd)
    with selectors.DefaultSelector() as selector:
        selector.register(args.parent_fd, selectors.EVENT_READ)
        while process.poll() is None:
            if selector.select(timeout=0.05) and not os.read(args.parent_fd, 1):
                os.killpg(os.getpgrp(), signal.SIGTERM)
                end = time.monotonic() + args.cleanup_seconds
                while process.poll() is None and time.monotonic() < end:
                    time.sleep(0.01)
                if process.poll() is None:
                    # The child also owns the slot FD; group death cannot make
                    # that slot available while its computation is still alive.
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                break
    returncode = process.wait()
    if returncode < 0:
        signum = -returncode
        if signum not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
