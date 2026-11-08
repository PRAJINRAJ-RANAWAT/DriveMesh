"""Hierarchical namespace stored only in the metadata DB (ADR-013).

Paths are absolute, '/'-separated. Folders form a tree via parent_id; files reference their
folder. Rename/move is a metadata-only update.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from drivemesh.db import File, Folder
from drivemesh.errors import AlreadyExists, Conflict, InvalidPath, NotFound

MAX_NAME_BYTES = 255
_FORBIDDEN_CHARS = {"\x00", "\\"}


def validate_name(name: str) -> str:
    if not name or name in (".", ".."):
        raise InvalidPath(f"invalid name: {name!r}")
    if len(name.encode()) > MAX_NAME_BYTES:
        raise InvalidPath("name too long")
    if "/" in name or any(c in name for c in _FORBIDDEN_CHARS) or any(ord(c) < 32 for c in name):
        raise InvalidPath(f"invalid characters in name: {name!r}")
    return name


def parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise InvalidPath("path must be absolute (start with '/')")
    stripped = path.strip("/")
    if not stripped:
        return []
    segs = stripped.split("/")
    for s in segs:
        validate_name(s)
    return segs


def normalize(path: str) -> str:
    return "/" + "/".join(parts(path))


def split(path: str) -> tuple[str, str]:
    segs = parts(path)
    if not segs:
        raise InvalidPath("path refers to the root folder")
    return "/" + "/".join(segs[:-1]), segs[-1]


def root_folder(s: Session, user_id: str) -> Folder:
    root = s.scalars(
        select(Folder).where(Folder.user_id == user_id, Folder.parent_id.is_(None))
    ).first()
    if root is None:
        root = Folder(user_id=user_id, parent_id=None, name="")
        s.add(root)
        s.flush()
    return root


def child_folder(s: Session, user_id: str, parent_id: str, name: str) -> Folder | None:
    return s.scalars(
        select(Folder).where(
            Folder.user_id == user_id, Folder.parent_id == parent_id, Folder.name == name
        )
    ).first()


def live_file(s: Session, user_id: str, folder_id: str, name: str) -> File | None:
    return s.scalars(
        select(File).where(
            File.user_id == user_id,
            File.folder_id == folder_id,
            File.name == name,
            File.state == "committed",
        )
    ).first()


def resolve_folder(s: Session, user_id: str, path: str, *, create: bool = False) -> Folder:
    folder = root_folder(s, user_id)
    for seg in parts(path):
        nxt = child_folder(s, user_id, folder.id, seg)
        if nxt is None:
            if not create:
                raise NotFound(f"folder not found: {path}")
            if live_file(s, user_id, folder.id, seg):
                raise Conflict(f"a file named {seg!r} exists")
            nxt = Folder(user_id=user_id, parent_id=folder.id, name=seg)
            s.add(nxt)
            s.flush()
        folder = nxt
    return folder


def folder_path(s: Session, folder: Folder) -> str:
    segs: list[str] = []
    cur: Folder | None = folder
    while cur is not None and cur.parent_id is not None:
        segs.append(cur.name)
        cur = s.get(Folder, cur.parent_id)
    return "/" + "/".join(reversed(segs))


def find_file(s: Session, user_id: str, path: str) -> File:
    parent, name = split(path)
    folder = resolve_folder(s, user_id, parent)
    f = live_file(s, user_id, folder.id, name)
    if f is None:
        raise NotFound(f"file not found: {path}")
    return f


def mkdir(s: Session, user_id: str, path: str, *, parents: bool = False) -> Folder:
    parent, name = split(path)
    pfolder = resolve_folder(s, user_id, parent, create=parents)
    if child_folder(s, user_id, pfolder.id, name):
        raise AlreadyExists(f"folder exists: {path}")
    if live_file(s, user_id, pfolder.id, name):
        raise Conflict(f"a file exists at {path}")
    f = Folder(user_id=user_id, parent_id=pfolder.id, name=name)
    s.add(f)
    s.flush()
    return f


def listdir(s: Session, user_id: str, path: str) -> tuple[list[Folder], list[File]]:
    folder = resolve_folder(s, user_id, path)
    folders = list(
        s.scalars(
            select(Folder)
            .where(Folder.user_id == user_id, Folder.parent_id == folder.id)
            .order_by(Folder.name)
        )
    )
    files = list(
        s.scalars(
            select(File)
            .where(File.user_id == user_id, File.folder_id == folder.id, File.state == "committed")
            .order_by(File.name)
        )
    )
    return folders, files


def subtree_folder_ids(s: Session, user_id: str, folder: Folder) -> list[str]:
    ids, frontier = [folder.id], [folder.id]
    while frontier:
        children = list(
            s.scalars(
                select(Folder.id).where(Folder.user_id == user_id, Folder.parent_id.in_(frontier))
            )
        )
        ids.extend(children)
        frontier = children
    return ids


def move(s: Session, user_id: str, src: str, dst: str) -> None:
    sparent, sname = split(src)
    dparent, dname = split(dst)
    sfolder = resolve_folder(s, user_id, sparent)
    dfolder = resolve_folder(s, user_id, dparent)
    if child_folder(s, user_id, dfolder.id, dname) or live_file(s, user_id, dfolder.id, dname):
        raise AlreadyExists(f"destination exists: {dst}")
    f = live_file(s, user_id, sfolder.id, sname)
    if f is not None:
        f.folder_id, f.name = dfolder.id, dname
        s.flush()
        return
    d = child_folder(s, user_id, sfolder.id, sname)
    if d is None:
        raise NotFound(f"not found: {src}")
    if dfolder.id in subtree_folder_ids(s, user_id, d):
        raise Conflict("cannot move a folder into itself")
    d.parent_id, d.name = dfolder.id, dname
    s.flush()
