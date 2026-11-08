"""REST API (/api/v1). Every handler is scoped to the logged-in user."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from drivemesh import metrics
from drivemesh.core import namespace as ns
from drivemesh.core.reader import FileReader
from drivemesh.core.workers import cluster_status, start_evacuation
from drivemesh.db import File, LinkedAccount, User
from drivemesh.errors import Forbidden, InvalidRequest, NotFound, TooLarge, Unauthorized
from drivemesh.web.security import ensure_csrf_token, require_csrf

router = APIRouter(prefix="/api/v1")
UPLOAD_BUFFER = 1 << 20


def _dm(request: Request):
    return request.app.state.dm


def file_json(f: File, path: str) -> dict[str, Any]:
    return {
        "id": f.id,
        "name": f.name,
        "path": path,
        "size": f.size,
        "sha256": f.sha256,
        "version": f.version,
        "chunks": f.chunk_count,
        "scheme": f.scheme,
        "rf": f.rf if f.scheme == "replica" else None,
        "ec": [f.ec_k, f.ec_m] if f.scheme == "ec" else None,
        "committed_at": f.committed_at.isoformat() + "Z" if f.committed_at else None,
    }


# ---- auth ---------------------------------------------------------------------------------------
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/auth/login")
def login(body: LoginBody, request: Request):
    dm = _dm(request)
    client = request.client.host if request.client else "unknown"
    if not request.app.state.login_throttle.allow(client):
        return JSONResponse(
            {"error": {"code": "rate_limited", "message": "too many login attempts"}},
            status_code=429,
        )
    user = dm.authenticate(body.username, body.password)
    request.session.clear()  # new session on login (session fixation defence)
    request.session["uid"] = user.id
    token = ensure_csrf_token(request)
    dm.event("login", user_id=user.id, client=client)
    return {"user": {"id": user.id, "username": user.username}, "csrf_token": token}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, user: User = Depends(require_csrf)):
    request.session.clear()
    return Response(status_code=204)


@router.get("/auth/me")
def me(request: Request):
    uid = request.session.get("uid")
    if not uid:
        raise Unauthorized("login required")
    dm = _dm(request)
    with dm.session() as s:
        user = s.get(User, uid)
    if user is None:
        raise Unauthorized("login required")
    return {
        "user": {"id": user.id, "username": user.username},
        "csrf_token": ensure_csrf_token(request),
        "google_configured": bool(dm.settings.google_client_id),
        "chunk_size": dm.settings.chunk_size,
        "allow_local_backends": dm.settings.allow_local_backends,
    }


# ---- namespace ----------------------------------------------------------------------------------
def file_health(dm, files: list[File]) -> dict[str, str]:
    """Replica health per file: healthy | repairing | under_replicated | unavailable."""
    from sqlalchemy import String, and_, case, cast, func, select

    from drivemesh.core.accounts import READABLE_STATES
    from drivemesh.db import Chunk, LinkedAccount, Replica

    if not files:
        return {}
    ids = [f.id for f in files]
    by_id = {f.id: f for f in files}
    readable = and_(Replica.state == "ok", LinkedAccount.state.in_(READABLE_STATES))
    unit = case((File.scheme == "ec", cast(Replica.shard_idx, String)), else_=Replica.id)
    healthy = func.count(func.distinct(case((readable, unit), else_=None)))
    pending = func.sum(case((Replica.state == "pending", 1), else_=0))
    with dm.session() as s:
        rows = s.execute(
            select(Chunk.file_id, healthy, pending)
            .join(File, File.id == Chunk.file_id)
            .outerjoin(Replica, Replica.chunk_id == Chunk.id)
            .outerjoin(LinkedAccount, LinkedAccount.id == Replica.account_id)
            .where(Chunk.file_id.in_(ids))
            .group_by(Chunk.id, Chunk.file_id)
        ).all()
    out = dict.fromkeys(ids, "healthy")
    rank = {"healthy": 0, "repairing": 1, "under_replicated": 2, "unavailable": 3}
    for fid, n, pend in rows:
        f = by_id[fid]
        need = f.ec_k if f.scheme == "ec" else 1
        target = (f.ec_k or 0) + (f.ec_m or 0) if f.scheme == "ec" else f.rf
        state = "healthy"
        if n < need:
            state = "unavailable"
        elif n < target:
            state = "repairing" if pend else "under_replicated"
        if rank[state] > rank[out[fid]]:
            out[fid] = state
    return out


@router.get("/fs")
def list_folder(request: Request, path: str = "/", user: User = Depends(require_csrf)):
    dm = _dm(request)
    norm = ns.normalize(path)
    folders, files = dm.listdir(user.id, norm)
    base = "" if norm == "/" else norm
    health = file_health(dm, files)
    return {
        "path": norm,
        "folders": [{"name": d.name, "path": f"{base}/{d.name}"} for d in folders],
        "files": [
            {**file_json(f, f"{base}/{f.name}"), "health": health.get(f.id, "healthy")}
            for f in files
        ],
    }


class PathBody(BaseModel):
    path: str
    parents: bool = False


@router.post("/folders", status_code=201)
def mkdir(body: PathBody, request: Request, user: User = Depends(require_csrf)):
    _dm(request).mkdir(user.id, body.path, parents=body.parents)
    return {"path": ns.normalize(body.path)}


@router.delete("/folders", status_code=202)
def rmdir(request: Request, path: str, recursive: bool = False, user: User = Depends(require_csrf)):
    n = _dm(request).delete_folder(user.id, path, recursive=recursive)
    return {"files_deleted": n}


class MoveBody(BaseModel):
    src: str
    dst: str


@router.post("/fs/move")
def move(body: MoveBody, request: Request, user: User = Depends(require_csrf)):
    _dm(request).move(user.id, body.src, body.dst)
    return {"src": body.src, "dst": body.dst}


# ---- files --------------------------------------------------------------------------------------
@router.put("/files/content", status_code=201)
async def upload(
    request: Request,
    path: str,
    overwrite: bool = False,
    rf: int | None = None,
    ec: str | None = None,
    user: User = Depends(require_csrf),
):
    dm = _dm(request)
    limit = dm.settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise TooLarge(f"upload exceeds {limit} bytes")
    kwargs: dict[str, Any] = {"overwrite": overwrite}
    if ec:
        m = re.fullmatch(r"(\d+),(\d+)", ec)
        if not m:
            raise InvalidRequest("ec must look like 'k,m'")
        kwargs.update(scheme="ec", ec_k=int(m.group(1)), ec_m=int(m.group(2)))
    elif rf is not None:
        kwargs["rf"] = rf
    progress_key = None
    upload_id = request.headers.get("x-upload-id", "")
    if _UPLOAD_ID.fullmatch(upload_id):
        progress_key = (user.id, upload_id)
        stored: set[int] = set()
        request.app.state.upload_progress[progress_key] = stored
        kwargs["on_chunk_stored"] = stored.add
    try:
        return await _stream_upload(request, dm, user, path, kwargs)
    finally:
        if progress_key is not None:
            request.app.state.upload_progress.pop(progress_key, None)


_UPLOAD_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


@router.get("/uploads/{upload_id}")
def upload_progress(upload_id: str, request: Request, user: User = Depends(require_csrf)):
    """Chunk indices already encrypted and stored on their accounts for an in-flight upload."""
    stored = request.app.state.upload_progress.get((user.id, upload_id))
    if stored is None:
        raise NotFound("no upload in progress with this id")
    return {"stored_chunks": sorted(stored)}


async def _stream_upload(request: Request, dm, user: User, path: str, kwargs: dict[str, Any]):
    writer = await run_in_threadpool(dm.open_writer, user.id, path, **kwargs)
    buf = bytearray()
    try:
        async for piece in request.stream():
            buf += piece
            if len(buf) >= UPLOAD_BUFFER:
                data = bytes(buf)
                buf.clear()
                await run_in_threadpool(writer.write, data)  # backpressure: awaits the pipeline
        if buf:
            await run_in_threadpool(writer.write, bytes(buf))
        info = await run_in_threadpool(writer.close)
    except BaseException as exc:
        if not writer._closed:
            await run_in_threadpool(writer.abort, f"upload interrupted: {type(exc).__name__}")
        raise
    return {
        "id": info.id,
        "path": info.path,
        "size": info.size,
        "sha256": info.sha256,
        "version": info.version,
        "chunks": info.chunks,
        "scheme": info.scheme,
        "rf": info.rf,
    }


_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    m = _RANGE.match(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):  # suffix: last N bytes
        n = int(m.group(2))
        if n == 0:
            return None
        return max(size - n, 0), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


@router.get("/files/content")
def download(request: Request, path: str, user: User = Depends(require_csrf)):
    dm = _dm(request)
    f = dm.stat(user.id, path)
    reader = FileReader(dm, f)
    size = f.size or 0
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(f.name)}",
        "ETag": f'"{f.sha256}"',
    }
    rng = request.headers.get("range")
    if rng and size:
        r = parse_range(rng, size)
        if r is None:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        start, end = r
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(
            reader.iter_range(start, end),
            status_code=206,
            media_type="application/octet-stream",
            headers=headers,
        )
    headers["Content-Length"] = str(size)
    return StreamingResponse(
        reader.iter_range(), media_type="application/octet-stream", headers=headers
    )


@router.get("/files/meta")
def file_meta(request: Request, path: str, user: User = Depends(require_csrf)):
    from sqlalchemy import select

    from drivemesh.db import Chunk, Replica

    dm = _dm(request)
    f = dm.stat(user.id, path)
    with dm.session() as s:
        rows = s.execute(
            select(Chunk.idx, Replica.account_id, Replica.state, Replica.shard_idx)
            .join(Replica, Replica.chunk_id == Chunk.id)
            .where(Chunk.file_id == f.id)
            .order_by(Chunk.idx)
        ).all()
    chunks: dict[int, list[dict[str, Any]]] = {}
    for idx, acc, state, shard in rows:
        chunks.setdefault(idx, []).append({"account_id": acc, "state": state, "shard": shard})
    return {
        **file_json(f, ns.normalize(path)),
        "chunk_replicas": [{"idx": i, "replicas": r} for i, r in sorted(chunks.items())],
    }


@router.delete("/files", status_code=202)
def delete_file(request: Request, path: str, user: User = Depends(require_csrf)):
    fid = _dm(request).delete_file(user.id, path)
    return {"id": fid, "state": "deleting"}


# ---- accounts -----------------------------------------------------------------------------------
def account_json(a: LinkedAccount) -> dict[str, Any]:
    return {
        "id": a.id,
        "provider": a.provider,
        "name": a.display_name,
        "email": a.email,
        "state": a.state,
        "capacity_bytes": a.capacity_bytes,
        "used_bytes": a.used_bytes,
        "free_bytes": a.free_bytes,
        "last_health_at": a.last_health_at.isoformat() + "Z" if a.last_health_at else None,
        "last_error": a.last_error,
    }


@router.get("/accounts")
def list_accounts(request: Request, user: User = Depends(require_csrf)):
    return {"accounts": [account_json(a) for a in _dm(request).accounts.list(user.id)]}


class LocalAccountBody(BaseModel):
    path: str = Field(min_length=1)
    name: str | None = None
    capacity_bytes: int | None = Field(default=None, gt=0)


@router.post("/accounts/local", status_code=201)
def add_local(body: LocalAccountBody, request: Request, user: User = Depends(require_csrf)):
    dm = _dm(request)
    if not dm.settings.allow_local_backends:
        raise Forbidden("local backends are disabled on this deployment")
    acc = dm.accounts.link_local(user.id, body.path, body.name, body.capacity_bytes)
    from drivemesh.core.workers import health_check

    health_check(dm)
    return account_json(dm.accounts.get(acc.id))


@router.delete("/accounts/{account_id}", status_code=202)
def remove_account(account_id: str, request: Request, user: User = Depends(require_csrf)):
    dm = _dm(request)
    acc = dm.accounts.get(account_id)
    if acc.user_id != user.id:
        raise NotFound("account not found")  # never reveal other users' accounts
    job_id = start_evacuation(dm, user.id, account_id)
    return {"account_id": account_id, "state": "draining", "job_id": job_id}


# ---- cluster ------------------------------------------------------------------------------------
@router.get("/cluster/status")
def status(request: Request, user: User = Depends(require_csrf)):
    return cluster_status(_dm(request), user.id)


__all__ = ["metrics", "router"]
