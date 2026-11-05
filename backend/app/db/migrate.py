"""Run Alembic migrations programmatically (used at startup and in tests)."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[3]


def upgrade(database_url: str | None = None) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend/app/db/migrations"))
    cfg.attributes["skip_logging"] = True
    if database_url:
        cfg.attributes["database_url"] = database_url
    command.upgrade(cfg, "head")
