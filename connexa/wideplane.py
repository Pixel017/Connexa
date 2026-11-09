"""Wide-plane endpoints for the two newest Common Model categories — ATS and
File Storage — completing six-of-six unified categories.

Same contract as the original four categories (auth, cursor'd lists, get,
create, PATCH-backed updates, write-schema discovery, remote_data embedding,
field scopes, webhooks, metering, audit):
    Authorization: Bearer <data API key>
    X-Account-Token: <per-linked-account token>
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .common_models import CANDIDATE_STATUSES, COMMON_MODELS
from .connectors.registry import registry
from .db import get_db
from .events import emit
from .metering import audit, usage
from .models import Candidate, FileRow, Folder, Org
from .security import data_plane_key
from .utils import iso, now, uid
from .unified import (_account, _apply_field_scopes, _connector_ctx, _minimize,
                      _next_cursor, _next_remote)

router = APIRouter(prefix="/api")

# ------------------------------------------------------------ passthrough (every category)
#
# Same contract on all six categories now: one POST, raw provider call, raw
# provider response. On the real lane (github-issues) this is a live forward;
# demo providers return their provider-shaped view marked demo=true.


def _register_passthrough(category: str) -> None:
    def endpoint(data: dict, auth=Depends(data_plane_key), db: Session = Depends(get_db),
                 account_token: str | None = Header(default=None, alias="X-Account-Token")):
        key, org = auth
        account = _account(account_token, db, org)
        connector = registry.require(account.integration)
        ctx = _connector_ctx(account)
        ctx.request_payload = data.get("payload")     # optional JSON body for provider POSTs
        method = str(data.get("method", "GET")).upper()
        path = str(data.get("path", "/"))
        raw = connector.passthrough(ctx, method, path)
        usage(db, plane="data", kind="passthrough", org_id=org.id, key_id=key.id,
              units=1.0, credit_type="medium")
        return raw
    endpoint.__name__ = f"{category}_passthrough"
    router.add_api_route(f"/{category}/v1/passthrough", endpoint, methods=["POST"],
                         name=f"{category}_passthrough",
                         description=f"Raw provider call escape hatch for the {category} category.")


for _category in ("crm", "hris", "accounting", "ats", "filestorage"):
    _register_passthrough(_category)


# ------------------------------------------------------------ ATS / Candidate

def _row_to_candidate(c: Candidate) -> dict:
    return {"id": c.id, "remote_id": c.remote_id, "first_name": c.first_name,
            "last_name": c.last_name, "email": c.email, "current_company": c.current_company,
            "role": c.role, "location": c.location, "application_status": c.application_status,
            "recruiter": c.recruiter_name, "notes": c.notes, "tags": c.tags,
            "created_at": iso(c.created_at), "updated_at": iso(c.updated_at),
            "remote_data": c.remote_extra}


def _row_to_file(f: FileRow) -> dict:
    return {"id": f.id, "remote_id": f.remote_id, "name": f.name, "mime_type": f.mime_type,
            "size_bytes": f.size_bytes, "folder": f.folder, "uploaded_by": f.uploaded_by,
            "tags": f.tags, "created_at": iso(f.created_at), "updated_at": iso(f.updated_at),
            "remote_data": f.remote_extra}


def _row_to_folder(f: Folder) -> dict:
    return {"id": f.id, "remote_id": f.remote_id, "name": f.name, "parent": f.parent,
            "created_at": iso(f.created_at), "updated_at": iso(f.updated_at),
            "remote_data": f.remote_extra}


class CandidateCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str | None = None
    email: str | None = None
    current_company: str | None = None
    role: str | None = None
    location: str | None = None
    application_status: str = "applied"
    recruiter: str | None = None
    notes: str | None = None
    tags: list[str] = Field(default_factory=list)


class CandidateUpdate(BaseModel):
    application_status: str | None = None
    role: str | None = None
    location: str | None = None
    recruiter: str | None = None
    notes: str | None = None
    tags: list[str] | None = None


class FileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    mime_type: str | None = None
    folder: str | None = None
    tags: list[str] = Field(default_factory=list)


class FileUpdate(BaseModel):
    name: str | None = None
    mime_type: str | None = None
    folder: str | None = None
    tags: list[str] | None = None


@router.get("/ats/v1/candidates")
def list_candidates(limit: int = 20, offset: int = 0, include_remote_data: bool = True,
                    auth=Depends(data_plane_key), db: Session = Depends(get_db),
                    account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Candidate).filter(Candidate.linked_account_id == account.id)
            .order_by(Candidate.created_at.desc()).limit(limit).offset(offset).all())
    usage(db, plane="data", kind="candidates_list", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return {"count": len(rows), "next": _next_cursor(limit, offset),
            "results": [_minimize(_apply_field_scopes(key, "Candidate", _row_to_candidate(r)),
                                  include_remote_data) for r in rows]}


@router.get("/ats/v1/candidates/{candidate_id}")
def get_candidate(candidate_id: str, include_remote_data: bool = True, auth=Depends(data_plane_key),
                  db: Session = Depends(get_db),
                  account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(Candidate).filter(Candidate.id == candidate_id,
                                     Candidate.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No Candidate found with id={candidate_id}."})
    return _minimize(_apply_field_scopes(key, "Candidate", _row_to_candidate(row)), include_remote_data)


@router.post("/ats/v1/candidates", status_code=201)
def create_candidate(data: CandidateCreate, background: BackgroundTasks, auth=Depends(data_plane_key),
                     db: Session = Depends(get_db),
                     account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    if data.application_status not in CANDIDATE_STATUSES:
        raise HTTPException(400, {"detail": [
            {"field": "application_status",
             "message": f"'{data.application_status}' is not a valid choice.",
             "allowed": CANDIDATE_STATUSES}]})
    row = Candidate(id=uid(), linked_account_id=account.id,
                    remote_id=f"CAN-{_next_remote(account.id)}",
                    first_name=data.first_name, last_name=data.last_name, email=data.email,
                    current_company=data.current_company, role=data.role, location=data.location,
                    application_status=data.application_status,
                    recruiter_name=data.recruiter, notes=data.notes, tags=data.tags,
                    remote_extra={"source": account.integration, "written": True})
    db.add(row)
    account.last_synced_at = now()
    db.commit()
    emit(db, background, event="Candidate.synced", org_id=org.id, payload_data={
        "candidate_id": row.id, "remote_id": row.remote_id,
        "application_status": row.application_status, "integration": account.integration})
    usage(db, plane="data", kind="candidate_write", org_id=org.id, key_id=key.id,
          units=1.0, credit_type="medium")
    audit(db, event_type="CANDIDATE_CREATED", description=row.remote_id, org_id=org.id, key_id=key.id)
    return _row_to_candidate(row)


@router.patch("/ats/v1/candidates/{candidate_id}")
def patch_candidate(candidate_id: str, data: CandidateUpdate, background: BackgroundTasks,
                    auth=Depends(data_plane_key), db: Session = Depends(get_db),
                    account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(Candidate).filter(Candidate.id == candidate_id,
                                     Candidate.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No Candidate found with id={candidate_id}."})
    if data.application_status and data.application_status not in CANDIDATE_STATUSES:
        raise HTTPException(400, {"detail": [
            {"field": "application_status",
             "message": f"'{data.application_status}' is not a valid choice.",
             "allowed": CANDIDATE_STATUSES}]})
    for name, value in (("application_status", data.application_status), ("role", data.role),
                        ("location", data.location), ("recruiter_name", data.recruiter),
                        ("notes", data.notes), ("tags", data.tags)):
        if value is not None:
            setattr(row, name, value)
    row.updated_at = now()
    db.commit()
    emit(db, background, event="Candidate.synced", org_id=org.id, payload_data={
        "candidate_id": row.id, "remote_id": row.remote_id,
        "application_status": row.application_status, "integration": account.integration})
    usage(db, plane="data", kind="candidate_write", org_id=org.id, key_id=key.id,
          units=1.0, credit_type="low")
    return _row_to_candidate(row)


@router.get("/ats/v1/meta/write-schema")
def ats_meta(auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    _key, _org = auth
    return {"category": "ats", "Candidate": COMMON_MODELS["ats"]["Candidate"]}


# ------------------------------------------------------------ File Storage / File + Folder

@router.get("/filestorage/v1/files")
def list_files(limit: int = 20, offset: int = 0, include_remote_data: bool = True,
               auth=Depends(data_plane_key), db: Session = Depends(get_db),
               account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(FileRow).filter(FileRow.linked_account_id == account.id)
            .order_by(FileRow.created_at.desc()).limit(limit).offset(offset).all())
    usage(db, plane="data", kind="files_list", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return {"count": len(rows), "next": _next_cursor(limit, offset),
            "results": [_minimize(_apply_field_scopes(key, "File", _row_to_file(r)),
                                  include_remote_data) for r in rows]}


@router.get("/filestorage/v1/files/{file_id}")
def get_file(file_id: str, include_remote_data: bool = True, auth=Depends(data_plane_key),
             db: Session = Depends(get_db),
             account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(FileRow).filter(FileRow.id == file_id,
                                   FileRow.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No File found with id={file_id}."})
    return _minimize(_apply_field_scopes(key, "File", _row_to_file(row)), include_remote_data)


@router.post("/filestorage/v1/files", status_code=201)
def create_file(data: FileCreate, background: BackgroundTasks, auth=Depends(data_plane_key),
                db: Session = Depends(get_db),
                account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = FileRow(id=uid(), linked_account_id=account.id,
                  remote_id=f"FILE-{_next_remote(account.id)}",
                  name=data.name, mime_type=data.mime_type, folder=data.folder, tags=data.tags,
                  remote_extra={"source": account.integration, "written": True})
    db.add(row)
    account.last_synced_at = now()
    db.commit()
    emit(db, background, event="File.synced", org_id=org.id, payload_data={
        "file_id": row.id, "remote_id": row.remote_id, "name": row.name,
        "integration": account.integration})
    usage(db, plane="data", kind="file_write", org_id=org.id, key_id=key.id,
          units=1.0, credit_type="medium")
    audit(db, event_type="FILE_CREATED", description=row.remote_id, org_id=org.id, key_id=key.id)
    return _row_to_file(row)


@router.patch("/filestorage/v1/files/{file_id}")
def patch_file(file_id: str, data: FileUpdate, background: BackgroundTasks,
               auth=Depends(data_plane_key), db: Session = Depends(get_db),
               account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(FileRow).filter(FileRow.id == file_id,
                                   FileRow.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No File found with id={file_id}."})
    for name, value in (("name", data.name), ("mime_type", data.mime_type),
                        ("folder", data.folder), ("tags", data.tags)):
        if value is not None:
            setattr(row, name, value)
    row.updated_at = now()
    db.commit()
    emit(db, background, event="File.synced", org_id=org.id, payload_data={
        "file_id": row.id, "remote_id": row.remote_id, "name": row.name,
        "integration": account.integration})
    usage(db, plane="data", kind="file_write", org_id=org.id, key_id=key.id, units=1.0, credit_type="low")
    return _row_to_file(row)


@router.get("/filestorage/v1/folders")
def list_folders(limit: int = 20, offset: int = 0, include_remote_data: bool = True,
                 auth=Depends(data_plane_key), db: Session = Depends(get_db),
                 account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    rows = (db.query(Folder).filter(Folder.linked_account_id == account.id)
            .order_by(Folder.created_at.desc()).limit(limit).offset(offset).all())
    usage(db, plane="data", kind="folders_list", org_id=org.id, key_id=key.id, units=1.0, credits=0)
    return {"count": len(rows), "next": _next_cursor(limit, offset),
            "results": [_minimize(_apply_field_scopes(key, "Folder", _row_to_folder(r)),
                                  include_remote_data) for r in rows]}


@router.get("/filestorage/v1/folders/{folder_id}")
def get_folder(folder_id: str, include_remote_data: bool = True, auth=Depends(data_plane_key),
               db: Session = Depends(get_db),
               account_token: str | None = Header(default=None, alias="X-Account-Token")):
    key, org = auth
    account = _account(account_token, db, org)
    row = db.query(Folder).filter(Folder.id == folder_id,
                                  Folder.linked_account_id == account.id).first()
    if not row:
        raise HTTPException(404, {"detail": f"No Folder found with id={folder_id}."})
    return _minimize(_apply_field_scopes(key, "Folder", _row_to_folder(row)), include_remote_data)


@router.get("/filestorage/v1/meta/write-schema")
def filestorage_meta(auth=Depends(data_plane_key), db: Session = Depends(get_db)):
    _key, _org = auth
    return {"category": "filestorage", "File": COMMON_MODELS["filestorage"]["File"],
            "Folder": COMMON_MODELS["filestorage"]["Folder"]}
