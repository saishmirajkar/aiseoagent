"""FastAPI application: JSON API + the built React dashboard."""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.api import audits, datalayer, settings
from app.auth import router as auth_router
from app.config import env
from app.db.session import get_engine

log = logging.getLogger(__name__)
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app() -> FastAPI:
    e = env()
    app = FastAPI(title="SEO Inspector", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")
    secret = e.session_secret
    if not secret:
        secret = secrets.token_urlsafe(32)
        log.warning("SESSION_SECRET is not set: using a temporary secret (logins reset on restart)")
    secure = e.session_cookie_secure or bool(os.environ.get("REPLIT_DEPLOYMENT"))
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="seo_inspector",
                       max_age=7 * 24 * 3600, same_site="lax", https_only=secure)
    app.include_router(auth_router)
    app.include_router(audits.router)
    app.include_router(settings.router)
    app.include_router(datalayer.router)

    @app.get("/api/health", include_in_schema=False)
    def health() -> JSONResponse:
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            return JSONResponse({"ok": True, "version": __version__})
        except Exception as ex:
            return JSONResponse({"ok": False, "error": type(ex).__name__}, status_code=503)

    if DIST.is_dir():
        app.mount("/", StaticFiles(directory=DIST, html=True), name="dashboard")
    else:
        @app.get("/", include_in_schema=False)
        def no_frontend() -> HTMLResponse:
            return HTMLResponse("<h1>SEO Inspector API</h1><p>The dashboard is not built. "
                                "Run <code>npm --prefix frontend ci && npm --prefix frontend run build</code>. "
                                "API docs: <a href='/api/docs'>/api/docs</a></p>")
    return app


app = create_app()
