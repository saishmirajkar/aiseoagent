"""Report storage: Replit Object Storage when available, else a Postgres table."""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from functools import lru_cache

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.config import env
from app.db.models import Blob
from app.db.session import session_scope

log = logging.getLogger(__name__)


class BlobStore(ABC):
    name = "abstract"

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes | None: ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None: ...


class PostgresBlobStore(BlobStore):
    name = "postgres"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        with session_scope() as db:
            stmt = insert(Blob).values(key=key, data=data, content_type=content_type)
            db.execute(stmt.on_conflict_do_update(index_elements=[Blob.key],
                                                  set_={"data": data, "content_type": content_type}))

    def get(self, key: str) -> bytes | None:
        with session_scope() as db:
            b = db.get(Blob, key)
            return bytes(b.data) if b else None

    def delete_prefix(self, prefix: str) -> None:
        with session_scope() as db:
            db.execute(delete(Blob).where(Blob.key.startswith(prefix)))


class ReplitObjectStore(BlobStore):
    name = "replit"

    def __init__(self, bucket_id: str | None):
        from replit.object_storage import Client  # type: ignore
        self.client = Client(bucket_id=bucket_id) if bucket_id else Client()

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.upload_from_bytes(key, data)

    def get(self, key: str) -> bytes | None:
        try:
            return self.client.download_as_bytes(key)
        except Exception:
            return None

    def delete_prefix(self, prefix: str) -> None:
        for obj in self.client.list(prefix=prefix):
            self.client.delete(obj.name, ignore_not_found=True)


@lru_cache
def blob_store() -> BlobStore:
    e = env()
    want = e.blob_backend
    on_replit = bool(os.environ.get("REPL_ID") or os.environ.get("REPLIT_DEPLOYMENT"))
    if want == "replit" or (want == "auto" and on_replit):
        try:
            store = ReplitObjectStore(e.object_storage_bucket_id)
            log.info("Using Replit Object Storage for reports")
            return store
        except Exception as ex:
            if want == "replit":
                raise
            log.warning("Replit Object Storage unavailable (%s); using Postgres for reports", ex)
    return PostgresBlobStore()
