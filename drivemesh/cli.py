"""drivemesh command-line interface.

Configuration comes from DRIVEMESH_* environment variables (or .env). Commands act as the
user given by --user (default: $DRIVEMESH_CLI_USER or 'owner').
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from drivemesh import __version__

Handler = Callable[[argparse.Namespace], int]
COMMANDS: dict[str, Handler] = {}


def _human(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(n)


def open_dm(args: argparse.Namespace | None = None, *, migrate: bool = True):
    from drivemesh import migrate as mig
    from drivemesh.config import get_settings
    from drivemesh.core.system import DriveMesh
    from drivemesh.logs import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if migrate:
        mig.upgrade(settings.database_url)
    return DriveMesh(settings)


def _user_id(dm, args: argparse.Namespace) -> str:
    return dm.get_user(args.user).id


def cmd(name: str):
    def deco(fn: Handler) -> Handler:
        COMMANDS[name] = fn
        return fn

    return deco


# ---- setup -------------------------------------------------------------------------------------
@cmd("gen-key")
def _gen_key(args) -> int:
    from drivemesh.crypto import generate_key_spec

    print(generate_key_spec(args.key_id))
    print(
        "# Store this in DRIVEMESH_MASTER_KEYS and keep an OFFLINE copy. "
        "If it is lost, all data is unrecoverable.",
        file=sys.stderr,
    )
    return 0


@cmd("init")
def _init(args) -> int:
    from drivemesh import migrate as mig
    from drivemesh.config import get_settings

    mig.upgrade(get_settings().database_url)
    print("metadata schema is up to date")
    return 0


@cmd("create-user")
def _create_user(args) -> int:
    dm = open_dm(args)
    password = args.password or os.environ.get("DRIVEMESH_NEW_USER_PASSWORD")
    if not password:
        password = getpass.getpass("password: ")
        if password != getpass.getpass("repeat: "):
            print("passwords differ", file=sys.stderr)
            return 1
    u = dm.create_user(args.username, password)
    print(f"created user {u.username} ({u.id})")
    return 0


# ---- accounts ----------------------------------------------------------------------------------
@cmd("account-add-local")
def _account_add_local(args) -> int:
    dm = open_dm(args)
    acc = dm.accounts.link_local(_user_id(dm, args), args.path, args.name, args.capacity)
    print(f"linked {acc.provider} account {acc.display_name} ({acc.id})")
    return 0


@cmd("account-list")
def _account_list(args) -> int:
    dm = open_dm(args)
    for a in dm.accounts.list(_user_id(dm, args), include_removed=args.all):
        print(
            f"{a.id}  {a.provider:7} {a.state:11} free={_human(a.free_bytes):>10}  "
            f"{a.display_name} {a.email or ''}"
        )
    return 0


@cmd("account-remove")
def _account_remove(args) -> int:
    from drivemesh.core.workers import start_evacuation

    dm = open_dm(args)
    job = start_evacuation(dm, _user_id(dm, args), args.account_id)
    print(f"account is draining; evacuation job {job}. Run `drivemesh worker` to complete it.")
    return 0


# ---- files -------------------------------------------------------------------------------------
@cmd("put")
def _put(args) -> int:
    dm = open_dm(args)
    kwargs: dict[str, Any] = {"overwrite": args.overwrite}
    if args.ec:
        k, m = (int(x) for x in args.ec.split(","))
        kwargs.update(scheme="ec", ec_k=k, ec_m=m)
    elif args.rf:
        kwargs["rf"] = args.rf
    with open(args.local, "rb") as fh:
        info = dm.put(_user_id(dm, args), args.remote, fh, **kwargs)
    print(json.dumps(info.__dict__))
    return 0


@cmd("get")
def _get(args) -> int:
    dm = open_dm(args)
    reader = dm.open_reader(_user_id(dm, args), args.remote)
    out = sys.stdout.buffer if args.local == "-" else open(args.local, "wb")  # noqa: SIM115
    try:
        for piece in reader.iter_range():
            out.write(piece)
    finally:
        if out is not sys.stdout.buffer:
            out.close()
    return 0


@cmd("ls")
def _ls(args) -> int:
    dm = open_dm(args)
    folders, files = dm.listdir(_user_id(dm, args), args.path)
    for d in folders:
        print(f"d {'':>10}  {d.name}/")
    for f in files:
        print(
            f"- {_human(f.size):>10}  {f.name}  (v{f.version}, {f.chunk_count} chunks, "
            f"{'rf=' + str(f.rf) if f.scheme == 'replica' else f'ec={f.ec_k}+{f.ec_m}'})"
        )
    return 0


@cmd("mkdir")
def _mkdir(args) -> int:
    dm = open_dm(args)
    dm.mkdir(_user_id(dm, args), args.path, parents=args.parents)
    return 0


@cmd("mv")
def _mv(args) -> int:
    dm = open_dm(args)
    dm.move(_user_id(dm, args), args.src, args.dst)
    return 0


@cmd("rm")
def _rm(args) -> int:
    dm = open_dm(args)
    uid = _user_id(dm, args)
    if args.recursive:
        n = dm.delete_folder(uid, args.path, recursive=True)
        print(f"deleting {n} files")
    else:
        dm.delete_file(uid, args.path)
    print("purge scheduled; run `drivemesh jobs` or the worker to complete it")
    return 0


# ---- operations --------------------------------------------------------------------------------
@cmd("fsck")
def _fsck(args) -> int:
    from drivemesh.core.fsck import fsck

    dm = open_dm(args)
    rep = fsck(dm, deep=args.deep)
    print(json.dumps({**rep.__dict__, "clean": rep.clean}, indent=2))
    return 0 if rep.clean else 2


@cmd("jobs")
def _jobs(args) -> int:
    from drivemesh.core import gc

    dm = open_dm(args)
    gc.scan(dm)
    n = dm.jobs.run_until_idle()
    print(json.dumps({"ran": n, **dm.jobs.counts()}))
    return 0


@cmd("worker")
def _worker(args) -> int:
    from drivemesh.core.workers import WorkerSet

    dm = open_dm(args)
    ws = WorkerSet(dm)
    print("worker running (Ctrl-C to stop)", file=sys.stderr)
    ws.run_forever()
    return 0


@cmd("status")
def _status(args) -> int:
    from drivemesh.core.workers import cluster_status

    dm = open_dm(args)
    print(json.dumps(cluster_status(dm, _user_id(dm, args)), indent=2, default=str))
    return 0


@cmd("backup")
def _backup(args) -> int:
    from drivemesh.core.backup import backup_now

    dm = open_dm(args)
    print(json.dumps(backup_now(dm), default=str))
    return 0


@cmd("restore")
def _restore(args) -> int:
    from drivemesh.core.backup import restore_from_local_dir

    dm = open_dm(args, migrate=True)
    print(json.dumps(restore_from_local_dir(dm, Path(args.from_dir)), default=str))
    return 0


@cmd("rewrap")
def _rewrap(args) -> int:
    from drivemesh.core.keys import rewrap_all

    dm = open_dm(args)
    print(json.dumps(rewrap_all(dm)))
    return 0


@cmd("migrate-legacy")
def _migrate_legacy(args) -> int:
    from drivemesh.legacy_import import drive_fetcher, import_registry

    legacy = Path(args.legacy_dir)
    registry = json.loads((legacy / "registry.json").read_text())
    key = (legacy / ".key").read_bytes().strip()
    dm = open_dm(args)
    report = import_registry(
        dm,
        _user_id(dm, args),
        registry,
        key,
        drive_fetcher(legacy),
        dest_root=args.dest,
        dry_run=args.dry_run,
    )
    print(json.dumps(report.__dict__, indent=2))
    return 0 if not report.failed else 2


@cmd("serve")
def _serve(args) -> int:
    import uvicorn

    uvicorn.run(
        "drivemesh.web.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


# ---- parser ------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drivemesh", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--version", action="version", version=f"drivemesh {__version__}")
    p.add_argument("--user", default=os.environ.get("DRIVEMESH_CLI_USER", "owner"))
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("gen-key", help="generate a master key spec")
    s.add_argument("--key-id", default="k1")
    sub.add_parser("init", help="create/upgrade the metadata schema")
    s = sub.add_parser("create-user")
    s.add_argument("username")
    s.add_argument("--password")

    s = sub.add_parser("account-add-local", help="link a local directory as a storage account")
    s.add_argument("path")
    s.add_argument("--name")
    s.add_argument("--capacity", type=int, help="simulated capacity in bytes")
    s = sub.add_parser("account-list")
    s.add_argument("--all", action="store_true")
    s = sub.add_parser("account-remove", help="unlink an account (evacuates its data first)")
    s.add_argument("account_id")

    s = sub.add_parser("put")
    s.add_argument("local")
    s.add_argument("remote")
    s.add_argument("--overwrite", action="store_true")
    s.add_argument("--rf", type=int)
    s.add_argument("--ec", help="erasure coding k,m (e.g. 4,2)")
    s = sub.add_parser("get")
    s.add_argument("remote")
    s.add_argument("local", help="'-' for stdout")
    s = sub.add_parser("ls")
    s.add_argument("path", nargs="?", default="/")
    s = sub.add_parser("mkdir")
    s.add_argument("path")
    s.add_argument("-p", "--parents", action="store_true")
    s = sub.add_parser("mv")
    s.add_argument("src")
    s.add_argument("dst")
    s = sub.add_parser("rm")
    s.add_argument("path")
    s.add_argument("-r", "--recursive", action="store_true")

    s = sub.add_parser("fsck")
    s.add_argument("--deep", action="store_true", help="also check every object exists remotely")
    sub.add_parser("jobs", help="run GC scan and pending jobs once")
    sub.add_parser("worker", help="run background workers until interrupted")
    sub.add_parser("status")
    sub.add_parser("backup", help="write an encrypted metadata snapshot to every account")
    s = sub.add_parser("restore", help="restore metadata from a snapshot in a local account dir")
    s.add_argument("from_dir")
    sub.add_parser("rewrap", help="re-wrap all keys with the active master key")
    s = sub.add_parser("migrate-legacy", help="import files from the legacy prototype")
    s.add_argument("legacy_dir")
    s.add_argument("--dest", default="/legacy")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    return p


def main(argv: list[str] | None = None) -> int:
    from drivemesh.errors import DriveMeshError

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    try:
        return COMMANDS[args.cmd](args)
    except DriveMeshError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
