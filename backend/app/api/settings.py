"""Settings API: runtime settings (editable) and integration key status (read-only booleans)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import AuthDep
from app.config import RuntimeSettings, env
from app.db.session import get_db
from app.settings_store import load, save
from app.storage.blobs import blob_store

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[AuthDep])


@router.get("")
def get_settings(db: Session = Depends(get_db)) -> dict:
    return {"runtime": load(db).model_dump(), "defaults": RuntimeSettings().model_dump(),
            "schema": RuntimeSettings.model_json_schema(),
            "integrations": env().integration_status(), "report_storage": blob_store().name}


@router.put("")
def put_settings(patch: dict, db: Session = Depends(get_db)) -> dict:
    try:
        merged = save(db, patch)
    except ValidationError as e:
        raise HTTPException(422, detail=[{"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]}
                                         for err in e.errors()])
    return {"runtime": merged.model_dump()}
