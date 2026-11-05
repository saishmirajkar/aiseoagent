from __future__ import annotations

import os
import socket

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://seo:seo@localhost:5432/seo_test")


def _db_available() -> bool:
    try:
        import psycopg
        with psycopg.connect(TEST_DB.replace("+psycopg", ""), connect_timeout=2):
            return True
    except Exception:
        return False


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def database_url():
    if not _db_available():
        pytest.skip("Postgres test database not available (set TEST_DATABASE_URL)")
    os.environ["DATABASE_URL"] = TEST_DB
    from app.config import env
    from app.db.session import _factory, get_engine
    env.cache_clear()
    get_engine.cache_clear()
    _factory.cache_clear()
    from sqlalchemy import create_engine, text
    eng = create_engine(TEST_DB)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    eng.dispose()
    from app.db.migrate import upgrade
    upgrade(TEST_DB)
    return TEST_DB


@pytest.fixture
def db(database_url):
    from app.db.session import session_scope
    with session_scope() as s:
        yield s
