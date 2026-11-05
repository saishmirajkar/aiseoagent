"""Postgres-backed job queue.

* ``enqueue`` inserts a row.
* ``claim`` atomically takes the highest-priority runnable job with
  ``SELECT ... FOR UPDATE SKIP LOCKED`` so several workers could share the table safely.
* Workers send heartbeats; ``recover_stale`` re-queues jobs whose worker died.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import Job

STALE_AFTER = timedelta(minutes=3)


@dataclass
class ClaimedJob:
    id: int
    type: str
    payload: dict
    attempts: int
    max_attempts: int


def enqueue(db: Session, type_: str, payload: dict, *, priority: int = 0,
            run_after: datetime | None = None, max_attempts: int = 3) -> Job:
    job = Job(type=type_, payload=payload, priority=priority, max_attempts=max_attempts,
              run_after=run_after or datetime.now(timezone.utc))
    db.add(job)
    db.flush()
    return job


_CLAIM_SQL = text("""
    UPDATE jobs SET status = 'running', locked_by = :worker, locked_at = now(),
                    heartbeat_at = now(), attempts = attempts + 1
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'queued' AND run_after <= now()
        ORDER BY priority DESC, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, type, payload, attempts, max_attempts
""")


def claim(db: Session, worker_id: str) -> ClaimedJob | None:
    row = db.execute(_CLAIM_SQL, {"worker": worker_id}).first()
    db.commit()
    if row is None:
        return None
    return ClaimedJob(id=row.id, type=row.type, payload=row.payload,
                      attempts=row.attempts, max_attempts=row.max_attempts)


def heartbeat(db: Session, job_id: int, worker_id: str) -> None:
    db.execute(text("UPDATE jobs SET heartbeat_at = now() WHERE id = :id AND locked_by = :w"),
               {"id": job_id, "w": worker_id})
    db.commit()


def complete(db: Session, job_id: int) -> None:
    db.execute(text("UPDATE jobs SET status = 'done', finished_at = now(), locked_by = NULL "
                    "WHERE id = :id"), {"id": job_id})
    db.commit()


def fail(db: Session, job: ClaimedJob, error: str) -> str:
    """Retry with exponential backoff, or mark failed when attempts are exhausted."""
    if job.attempts < job.max_attempts:
        delay = min(600, 30 * 2 ** (job.attempts - 1))
        db.execute(text("UPDATE jobs SET status = 'queued', locked_by = NULL, last_error = :e, "
                        "run_after = now() + make_interval(secs => :d) WHERE id = :id"),
                   {"id": job.id, "e": error[:4000], "d": delay})
        state = "retry"
    else:
        db.execute(text("UPDATE jobs SET status = 'failed', finished_at = now(), locked_by = NULL, "
                        "last_error = :e WHERE id = :id"), {"id": job.id, "e": error[:4000]})
        state = "failed"
    db.commit()
    return state


def release(db: Session, job_id: int) -> None:
    """Give a job back without counting the attempt (graceful shutdown)."""
    db.execute(text("UPDATE jobs SET status = 'queued', locked_by = NULL, "
                    "attempts = GREATEST(attempts - 1, 0) WHERE id = :id AND status = 'running'"),
               {"id": job_id})
    db.commit()


def recover_stale(db: Session, stale_after: timedelta = STALE_AFTER) -> list[int]:
    """Re-queue running jobs whose heartbeat stopped (worker crash / VM restart)."""
    rows = db.execute(text("""
        UPDATE jobs SET
            status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
            finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
            locked_by = NULL,
            last_error = COALESCE(last_error, '') || ' [worker heartbeat lost]'
        WHERE status = 'running' AND heartbeat_at < now() - make_interval(secs => :s)
        RETURNING id, status
    """), {"s": int(stale_after.total_seconds())}).all()
    db.commit()
    return [r.id for r in rows]
