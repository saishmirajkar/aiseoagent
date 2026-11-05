"""Worker process: claims jobs from Postgres and runs them one at a time.

One audit at a time keeps us inside the memory budget (a single headless browser).
APScheduler runs periodic maintenance in the same process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, update

from app.config import env
from app.db.models import Audit, CacheEntry
from app.db.session import session_scope
from app.queue import jobs

log = logging.getLogger("worker")
POLL_S = 2.0
HEARTBEAT_S = 20.0

Handler = Callable[[dict], Awaitable[None]]


async def _audit_handler(payload: dict) -> None:
    from app.orchestrator import run_audit
    audit_id = int(payload["audit_id"])
    try:
        await run_audit(audit_id)
    except Exception as e:
        log.exception("Audit %s crashed", audit_id)

        def _fail() -> None:
            with session_scope() as db:
                db.execute(update(Audit).where(Audit.id == audit_id).values(
                    status="failed", error=f"Internal error: {type(e).__name__}: {e}"[:2000],
                    finished_at=datetime.now(timezone.utc)))
        await asyncio.to_thread(_fail)
        raise


HANDLERS: dict[str, Handler] = {"audit": _audit_handler}


def _cleanup_cache() -> None:
    with session_scope() as db:
        n = db.execute(delete(CacheEntry).where(CacheEntry.expires_at < datetime.now(timezone.utc))).rowcount
    log.info("Cache cleanup removed %s expired entries", n)


def _recover() -> None:
    with session_scope() as db:
        ids = jobs.recover_stale(db)
    if ids:
        log.warning("Re-queued stale jobs: %s", ids)


class Worker:
    def __init__(self) -> None:
        self.id = f"{socket.gethostname()}:{os.getpid()}"
        self.stopping = asyncio.Event()
        self.current: int | None = None

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await asyncio.to_thread(self._beat, job_id)

    def _beat(self, job_id: int) -> None:
        with session_scope() as db:
            jobs.heartbeat(db, job_id, self.id)

    def _claim(self) -> jobs.ClaimedJob | None:
        with session_scope() as db:
            return jobs.claim(db, self.id)

    async def run_one(self) -> bool:
        job = await asyncio.to_thread(self._claim)
        if job is None:
            return False
        self.current = job.id
        log.info("Job %s (%s) started, attempt %s/%s", job.id, job.type, job.attempts, job.max_attempts)
        hb = asyncio.create_task(self._heartbeat(job.id))
        try:
            handler = HANDLERS.get(job.type)
            if handler is None:
                raise ValueError(f"Unknown job type {job.type!r}")
            await handler(job.payload)
        except asyncio.CancelledError:
            await asyncio.to_thread(lambda: _with_db(lambda db: jobs.release(db, job.id)))
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            state = await asyncio.to_thread(lambda: _with_db(lambda db: jobs.fail(db, job, err)))
            log.error("Job %s failed (%s): %s", job.id, state, e)
        else:
            await asyncio.to_thread(lambda: _with_db(lambda db: jobs.complete(db, job.id)))
            log.info("Job %s done", job.id)
        finally:
            hb.cancel()
            self.current = None
        return True

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stopping.set)
            except NotImplementedError:
                pass
        await asyncio.to_thread(_recover)
        scheduler = AsyncIOScheduler(timezone="UTC")
        # sync callables run in APScheduler's thread pool, off the event loop
        scheduler.add_job(_recover, "interval", minutes=1, id="recover_stale")
        scheduler.add_job(_cleanup_cache, "interval", hours=24, id="cache_cleanup",
                          next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5))
        scheduler.start()
        log.info("Worker %s started", self.id)
        try:
            while not self.stopping.is_set():
                task = asyncio.create_task(self.run_one())
                stop = asyncio.create_task(self.stopping.wait())
                done, _ = await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
                if stop in done and not task.done():
                    log.info("Shutdown requested: releasing current job")
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    break
                stop.cancel()
                if not task.result():
                    try:
                        await asyncio.wait_for(self.stopping.wait(), timeout=POLL_S)
                    except asyncio.TimeoutError:
                        pass
        finally:
            scheduler.shutdown(wait=False)
            log.info("Worker stopped")


def _with_db(fn):
    with session_scope() as db:
        return fn(db)


def main() -> None:
    logging.basicConfig(level=env().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # one line per request is too much
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
