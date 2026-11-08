"""Browser smoke tests (Playwright + Chromium) against a real uvicorn server.

Skipped unless DRIVEMESH_UI_TESTS=1. Locally, point DRIVEMESH_UI_BROWSER at any Chromium-based
browser binary to skip Playwright's browser download. Screenshots go to ui-artifacts/.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DRIVEMESH_UI_TESTS") != "1",
    reason="set DRIVEMESH_UI_TESTS=1 to run browser tests",
)
sync_api = pytest.importorskip("playwright.sync_api")

PASSWORD = "ui-test-password"
ART = Path("ui-artifacts")
WIDTHS = {"desktop": (1440, 900), "phone": (390, 844), "small": (360, 760)}


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    import uvicorn

    from drivemesh import crypto
    from drivemesh.core.system import DriveMesh
    from drivemesh.db import make_engine
    from drivemesh.web.app import create_app
    from tests.conftest import make_settings

    tmp = tmp_path_factory.mktemp("ui")
    url = f"sqlite:///{tmp}/ui.db"
    dm = DriveMesh(
        make_settings(database_url=url, login_rate_per_minute=1000),
        keyring=crypto.KeyRing.parse(crypto.generate_key_spec()),
        engine=make_engine(url),
        sleep=lambda s: None,
    )
    dm.create_schema()
    user = dm.create_user("owner", PASSWORD)
    for name in ("a", "b", "c"):
        dm.accounts.link_local(user.id, str(tmp / name), f"acct-{name}")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(dm, start_workers=False), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    ART.mkdir(exist_ok=True)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    dm.close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        exe = os.environ.get("DRIVEMESH_UI_BROWSER")
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        yield b
        b.close()


def new_page(browser, size, theme="light", reduced=False):
    w, h = WIDTHS[size]
    ctx = browser.new_context(
        viewport={"width": w, "height": h},
        color_scheme=theme,
        reduced_motion="reduce" if reduced else "no-preference",
        is_mobile=w < 600,
        has_touch=w < 600,
    )
    page = ctx.new_page()
    page.errors = []
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    return ctx, page


def overflow(page) -> int:
    return page.evaluate("document.documentElement.scrollWidth - window.innerWidth")


def login(page, base_url):
    page.goto(base_url + "/login")
    page.fill("input[name=username]", "owner")
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/app")


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", list(WIDTHS))
def test_landing_page(browser, base_url, size, theme):
    ctx, page = new_page(browser, size, theme)
    page.goto(base_url + "/", wait_until="networkidle")
    assert page.evaluate("document.documentElement.dataset.theme") == theme
    height = page.evaluate("document.body.scrollHeight")
    for y in range(0, height, 600):  # scroll through every section and scene
        page.evaluate(f"window.scrollTo(0, {y})")
        time.sleep(0.03)
    story_end = page.evaluate(
        "(() => { const s = document.getElementById('story');"
        " return s.offsetTop + s.offsetHeight - innerHeight; })()"
    )
    page.evaluate(f"window.scrollTo(0, {story_end})")
    time.sleep(0.3)
    assert page.get_attribute("[data-story]", "data-phase") == "3"
    assert overflow(page) == 0, "horizontal overflow"
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.2)
    page.screenshot(path=str(ART / f"landing-{size}-{theme}.png"))
    page.click("[data-theme-toggle]")
    assert page.evaluate("document.documentElement.dataset.theme") != theme
    assert page.errors == []
    ctx.close()


def test_reduced_motion_shows_final_state(browser, base_url):
    ctx, page = new_page(browser, "desktop", reduced=True)
    page.goto(base_url + "/", wait_until="networkidle")
    assert page.get_attribute("[data-story]", "data-phase") == "3"
    assert page.get_attribute("[data-scene]", "data-phase") == "3"
    assert page.errors == []
    ctx.close()


@pytest.mark.parametrize("size", ["desktop", "phone"])
def test_app_pages(browser, base_url, size):
    ctx, page = new_page(browser, size)
    login(page, base_url)
    for path in ("/app", "/files", "/accounts", "/status"):
        page.goto(base_url + path, wait_until="networkidle")
        time.sleep(0.3)
        assert overflow(page) == 0, f"horizontal overflow on {path}"
        page.screenshot(path=str(ART / f"app{path.replace('/', '-')}-{size}.png"), full_page=True)
    assert page.errors == []
    ctx.close()


def test_upload_shows_per_chunk_progress(browser, base_url, tmp_path):
    ctx, page = new_page(browser, "desktop")
    login(page, base_url)
    page.goto(base_url + "/files", wait_until="networkidle")
    f = tmp_path / "ui-upload.bin"
    f.write_bytes(os.urandom(5 * 64 * 1024 + 7))  # 6 chunks at the test chunk size
    page.set_input_files("#file-input", str(f))
    page.wait_for_selector(".upload.done", timeout=30_000)
    assert "Stored" in page.text_content(".upload .upload-state")
    assert page.locator(".upload .chunks .chunk.stored").count() == 6
    page.wait_for_selector("text=ui-upload.bin")
    assert page.errors == []
    ctx.close()
