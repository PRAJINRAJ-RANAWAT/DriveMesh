"""In-memory fake of the Google Drive v3 HTTP API, speaking the wire protocol that the real
googleapiclient uses (static discovery doc, resumable uploads, media downloads).

This lets GoogleDriveBackend be tested with the real client library, offline: request
building, resumable session handling and HTTP error mapping are exercised for real.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
from urllib.parse import parse_qs, urlparse

import httplib2


class FakeDrive:
    def __init__(self, quota_limit: int | None = 15 * 1024**3):
        self.files: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.quota_limit = quota_limit
        self.errors: list[tuple[int, str]] = []  # injected (status, reason) for next requests
        self.requests: list[tuple[str, str]] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def inject(self, status: int, reason: str = "", times: int = 1) -> None:
        self.errors.extend([(status, reason)] * times)

    def usage(self) -> int:
        return sum(len(f.get("content", b"")) for f in self.files.values())

    def http(self) -> FakeHttp:
        return FakeHttp(self)

    def service(self):
        from googleapiclient.discovery import build

        return build("drive", "v3", http=self.http(), static_discovery=True)


def _resp(status: int, body: bytes | dict = b"", headers: dict | None = None):
    if isinstance(body, dict):
        body = json.dumps(body).encode()
    h = {"status": str(status), "content-type": "application/json"}
    h.update(headers or {})
    return httplib2.Response(h), body


def _error(status: int, reason: str):
    return _resp(
        status,
        {"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}},
    )


class FakeHttp:
    def __init__(self, drive: FakeDrive):
        self.d = drive

    def request(self, uri, method="GET", body=None, headers=None, **_kw):
        d = self.d
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        with d._lock:
            d.requests.append((method, uri))
            if d.errors:
                return _error(*d.errors.pop(0))
            u = urlparse(uri)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = u.path
            if path.startswith("/resumable/"):
                return self._resumable_put(path.split("/")[-1], body, headers)
            if path.endswith("/upload/drive/v3/files") and method == "POST":
                return self._start_upload(body, qs)
            if path.endswith("/drive/v3/files") and method == "POST":
                meta = json.loads(body or b"{}")
                return _resp(200, self._create(meta, b""))
            if path.endswith("/drive/v3/files") and method == "GET":
                return self._list(qs)
            if path.endswith("/drive/v3/about"):
                q = {"usage": str(d.usage())}
                if d.quota_limit is not None:
                    q["limit"] = str(d.quota_limit)
                return _resp(200, {"storageQuota": q, "user": {"emailAddress": "fake@example.com"}})
            m = re.search(r"/drive/v3/files/([^/?]+)$", path)
            if m:
                fid = m.group(1)
                f = d.files.get(fid)
                if f is None or f.get("trashed"):
                    return _error(404, "notFound")
                if method == "DELETE":
                    del d.files[fid]
                    return _resp(204, b"")
                if qs.get("alt") == "media":
                    data = f["content"]
                    return _resp(
                        200,
                        data,
                        {
                            "content-length": str(len(data)),
                            "content-type": "application/octet-stream",
                        },
                    )
                return _resp(200, {"id": fid, "name": f["name"]})
        return _error(400, "badRequest")

    # --- uploads ------------------------------------------------------------------------------
    def _create(self, meta: dict, content: bytes) -> dict:
        d = self.d
        if d.quota_limit is not None and d.usage() + len(content) > d.quota_limit:
            raise _QuotaError()
        fid = f"f{next(d._ids)}"
        d.files[fid] = {
            "name": meta.get("name"),
            "parents": meta.get("parents", []),
            "mimeType": meta.get("mimeType", "application/octet-stream"),
            "content": content,
            "createdTime": "2026-11-08T12:00:00.000Z",
        }
        return {"id": fid, "size": str(len(content))}

    def _start_upload(self, body, qs):
        if qs.get("uploadType") != "resumable":
            return _error(400, "only resumable uploads are supported by the fake")
        sid = f"s{next(self.d._ids)}"
        self.d.sessions[sid] = {"meta": json.loads(body or b"{}"), "data": bytearray()}
        return _resp(200, b"", {"location": f"https://fake.upload/resumable/{sid}"})

    def _resumable_put(self, sid, body, headers):
        sess = self.d.sessions.get(sid)
        if sess is None:
            return _error(404, "notFound")
        cr = headers.get("content-range", "")
        m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", cr)
        if m is None:
            # status query: "bytes */total"
            n = len(sess["data"])
            return _resp(308, b"", {"range": f"bytes=0-{n - 1}"} if n else {})
        start, _end, total = int(m.group(1)), int(m.group(2)), m.group(3)
        if start != len(sess["data"]):
            return _error(400, "badRange")
        if hasattr(body, "read"):
            body = body.read()
        sess["data"] += body if isinstance(body, bytes) else body.encode()
        if total != "*" and len(sess["data"]) >= int(total):
            try:
                res = self._create(sess["meta"], bytes(sess["data"]))
            except _QuotaError:
                return _error(403, "storageQuotaExceeded")
            del self.d.sessions[sid]
            return _resp(200, res)
        return _resp(308, b"", {"range": f"bytes=0-{len(sess['data']) - 1}"})

    # --- queries ------------------------------------------------------------------------------
    def _list(self, qs):
        q = qs.get("q", "")
        out = []
        for fid, f in self.d.files.items():
            ok = True
            for clause in re.split(r"\s+and\s+", q):
                clause = clause.strip()
                if not clause:
                    continue
                if m := re.match(r"name = '(.*)'$", clause):
                    ok &= f["name"] == m.group(1)
                elif m := re.match(r"name contains '(.*)'$", clause):
                    ok &= f["name"].startswith(m.group(1))
                elif m := re.match(r"mimeType = '(.*)'$", clause):
                    ok &= f["mimeType"] == m.group(1)
                elif m := re.match(r"'(.*)' in parents$", clause):
                    ok &= m.group(1) in f["parents"]
                elif clause == "trashed = false":
                    ok &= not f.get("trashed")
            if ok:
                out.append(
                    {
                        "id": fid,
                        "name": f["name"],
                        "size": str(len(f["content"])),
                        "createdTime": f["createdTime"],
                    }
                )
        page_size = int(qs.get("pageSize", 100))
        start = int(qs.get("pageToken", 0) or 0)
        page = out[start : start + page_size]
        body = {"files": page}
        if start + page_size < len(out):
            body["nextPageToken"] = str(start + page_size)
        return _resp(200, body)


class _QuotaError(Exception):
    pass
