"""Launcher for the Reserved VM: migrate, then run web + worker and restart them if they die.

    python -m app.run            # web + worker
    python -m app.run web        # web only
    python -m app.run worker     # worker only
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time

from app.config import env

log = logging.getLogger("launcher")


def _commands(which: str) -> dict[str, list[str]]:
    port = str(os.environ.get("PORT") or env().port)
    cmds = {
        "web": [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port,
                "--proxy-headers", "--forwarded-allow-ips", "*", "--log-level", env().log_level.lower()],
        "worker": [sys.executable, "-m", "app.queue.worker"],
    }
    return cmds if which == "all" else {which: cmds[which]}


def main() -> None:
    logging.basicConfig(level=env().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    from app.db.migrate import upgrade
    log.info("Applying database migrations")
    upgrade()

    procs: dict[str, subprocess.Popen] = {}
    backoff: dict[str, float] = {}
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        for p in procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cmds = _commands(which)
    for name, cmd in cmds.items():
        procs[name] = subprocess.Popen(cmd)
        backoff[name] = 1.0
        log.info("Started %s (pid %s)", name, procs[name].pid)

    while not stopping:
        time.sleep(1)
        for name, p in list(procs.items()):
            code = p.poll()
            if code is None or stopping:
                continue
            log.error("%s exited with code %s; restarting in %.0fs", name, code, backoff[name])
            time.sleep(backoff[name])
            backoff[name] = min(60.0, backoff[name] * 2)
            procs[name] = subprocess.Popen(cmds[name])

    deadline = time.time() + 30
    for p in procs.values():
        try:
            p.wait(timeout=max(1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    main()
