#!/usr/bin/env python3
"""Send a command to the running MALCIP process without importing Qt or NumPy."""

import errno
import os
import socket
import sys
from pathlib import Path


COMMANDS = {"toggle", "fluid", "system", "globe", "field", "scope", "config"}


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    if command not in COMMANDS:
        print(f"Unknown MALCIP command: {command}", file=sys.stderr)
        return 64
    path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/malcip-{os.getuid()}")) / "malcip.sock"
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.25)
    try:
        client.connect(str(path))
        client.sendall((command + "\n").encode("ascii"))
        return 0
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
            return 2  # No server: the launcher should start one.
        print(f"MALCIP control socket: {exc}", file=sys.stderr)
        return 3
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
