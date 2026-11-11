"""Daytona smoke test: create sandbox, run hello world, inspect, delete.

Usage:
    DAYTONA_API_KEY=... .venv/bin/python scripts/daytona_smoke.py
"""

import os
import sys

from daytona import Daytona, DaytonaConfig


def main() -> int:
    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        print("DAYTONA_API_KEY is not set")
        return 2

    daytona = Daytona(DaytonaConfig(api_key=api_key))

    sandbox = None
    try:
        print("== creating sandbox ==")
        sandbox = daytona.create()
        print("sandbox id:", sandbox.id)

        res = sandbox.process.exec("uname -a; python3 --version")
        print("== hello world ==")
        print(res.result.strip())

        res = sandbox.process.exec(
            'python3 -c "import platform; print(platform.system(), platform.release())"'
        )
        print("guest OS:", res.result.strip())
    finally:
        if sandbox is not None:
            sandbox.delete()
            print("== sandbox deleted ==")

    # What does this account/org offer? Probe for regions + windows support.
    try:
        print("== existing sandboxes ==")
        for s in daytona.list():
            print(" ", s.id, getattr(s, "state", "?"))
    except Exception as e:
        print("list() failed:", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
