"""FastAPI application factory."""

from __future__ import annotations

import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text
from starlette.middleware.sessions import SessionMiddleware

from drivemesh import metrics
from drivemesh.config import Settings, get_settings
from drivemesh.core.system import DriveMesh
from drivemesh.db import User
from drivemesh.errors import AlreadyExists, DriveMeshError
from drivemesh.web import api, facts, oauth
from drivemesh.web.security import LoginThrottle, RequestContextMiddleware

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _bootstrap_owner(dm: DriveMesh) -> None:
    st = dm.settings
    if not (st.owner_username and st.owner_password):
        return
    with dm.session() as s:
        if s.scalar(select(func.count()).select_from(User)):
            return
    try:
        dm.create_user(st.owner_username, st.owner_password.get_secret_value())
        log.warning("bootstrapped owner account", extra={"username": st.owner_username})
    except AlreadyExists:
        pass


def create_app(
    dm: DriveMesh | None = None,
    *,
    settings: Settings | None = None,
    start_workers: bool | None = None,
    run_migrations: bool = True,
) -> FastAPI:
    owns_dm = dm is None
    if dm is None:
        from drivemesh import migrate
        from drivemesh.logs import configure_logging

        settings = settings or get_settings()
        configure_logging(settings.log_level, settings.log_json)
        if run_migrations:
            migrate.upgrade(settings.database_url)
        dm = DriveMesh(settings)
    st = dm.settings
    if st.env == "prod" and (problems := st.production_problems()):
        raise RuntimeError("unsafe production configuration: " + "; ".join(problems))
    if st.session_secret is None:
        session_secret = secrets.token_urlsafe(48)
        log.warning(
            "no DRIVEMESH_SESSION_SECRET: using an ephemeral one (sessions reset on restart)"
        )
    else:
        session_secret = st.session_secret.get_secret_value()
    run_workers = st.workers_enabled if start_workers is None else start_workers

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _bootstrap_owner(dm)
        workers = None
        if run_workers:
            from drivemesh.core.workers import WorkerSet

            workers = WorkerSet(dm)
            workers.start()
        app.state.workers = workers
        try:
            yield
        finally:
            if workers:
                workers.stop()
            if owns_dm:
                dm.close()

    app = FastAPI(
        title="DriveMesh",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.dm = dm
    app.state.login_throttle = LoginThrottle(st.login_rate_per_minute)
    app.add_middleware(RequestContextMiddleware, hsts=st.secure_cookies)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="dm_session",
        same_site="lax",
        https_only=st.secure_cookies,
        max_age=14 * 24 * 3600,
    )
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.include_router(api.router)
    app.include_router(oauth.router)

    @app.exception_handler(DriveMeshError)
    async def _dm_error(request: Request, exc: DriveMeshError):
        if exc.http_status >= 500:
            log.error("request failed", extra={"code": exc.code, "error": str(exc)})
        if request.url.path.startswith("/accounts/") and exc.http_status != 401:
            return HTMLResponse(
                templates.get_template("error.html").render(message=str(exc)),
                status_code=exc.http_status,
            )
        if exc.http_status == 401 and not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(
            {"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.http_status
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": str(exc.errors())}}, status_code=422
        )

    def page(name: str, auth: bool = True):
        def handler(request: Request):
            if auth and not request.session.get("uid"):
                return RedirectResponse("/login", status_code=303)
            return templates.TemplateResponse(request, name, {"active": name.split(".")[0]})

        return handler

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request):
        """Public home page; 'Start meshing' leads to /login (or the dashboard when signed in)."""
        logged_in = bool(request.session.get("uid"))
        return templates.TemplateResponse(
            request,
            "landing.html",
            {
                "logged_in": logged_in,
                "cta_href": "/app" if logged_in else "/login",
                "test_count": facts.TEST_COUNT,
                "coverage": facts.COVERAGE,
            },
        )

    for path, tpl in (
        ("/app", "dashboard.html"),
        ("/files", "files.html"),
        ("/accounts", "accounts.html"),
        ("/status", "status.html"),
    ):
        app.add_api_route(
            path, page(tpl), methods=["GET"], response_class=HTMLResponse, include_in_schema=False
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if request.session.get("uid"):
            return RedirectResponse("/app", status_code=303)
        return templates.TemplateResponse(request, "login.html", {})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/favicon.svg", status_code=301)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        try:
            with dm.session() as s:
                s.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ready": False, "reason": f"db: {exc}"}, status_code=503)
        if dm.safe_mode.active:
            return JSONResponse(
                {"ready": False, "reason": f"safe mode: {dm.safe_mode.reason}"}, status_code=503
            )
        return {"ready": True}

    @app.get("/metrics")
    def prom(request: Request):
        token = st.metrics_token.get_secret_value() if st.metrics_token else None
        auth = request.headers.get("authorization", "")
        if token:
            if not hmac.compare_digest(auth, f"Bearer {token}"):
                return PlainTextResponse("unauthorized", status_code=401)
        elif not request.session.get("uid"):
            return PlainTextResponse("unauthorized", status_code=401)
        return PlainTextResponse(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app
