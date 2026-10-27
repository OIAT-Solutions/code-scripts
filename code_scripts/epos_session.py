"""One EPOS back-office login per Nora daily run (owner, 26 Oct 2026).

Every step that reads EPOS used to log in on its own (products, POs, bills, sales, repayments, stock changes,
stock snapshots: ~7 logins a night). ``daily_run`` sets ``OIAT_EPOS_SESSION_REUSE=1`` for itself and its child
processes. The first step logs in as before and saves the browser's EPOS cookies (never the password) to
``<STATE_ROOT>/epos_session/<company>.json`` (owner-only file). Later steps load them before opening their page
and log in again only if EPOS shows its login page. The file is ignored once older than ``MAX_AGE`` (refreshed
by each step that uses it) and deleted when the daily run ends. Without the flag nothing is saved or reused,
so other companies and manual tools behave exactly as before.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

ENV = "OIAT_EPOS_SESSION_REUSE"
MAX_AGE = 30 * 60
LOGIN_HOST = "login.eposnowhq.com"


def enabled() -> bool:
    return os.environ.get(ENV, "").strip() == "1"


def path(company_key: str) -> Path:
    from code_scripts.paths import STATE_ROOT

    return Path(os.environ.get("STATE_ROOT") or STATE_ROOT) / "epos_session" / f"{company_key}.json"


def restore(context, company_key: str) -> bool:
    """Load this run's saved EPOS cookies into ``context``; False when there is nothing fresh to reuse."""
    if not enabled():
        return False
    p = path(company_key)
    try:
        if time.time() - p.stat().st_mtime > MAX_AGE:
            return False
        cookies = json.loads(p.read_text()).get("cookies") or []
    except (OSError, ValueError):
        return False
    if not cookies:
        return False
    context.add_cookies(cookies)
    return True


def save(context, company_key: str) -> None:
    """Keep the logged-in cookies for the next step of this run (owner-only file, replaced atomically)."""
    if not enabled():
        return
    p = path(company_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"saved_at": time.time(), "cookies": context.cookies()}, fh)
    os.replace(tmp, p)


def on_login_page(page, timeout_ms: int = 20000) -> bool:
    """False only when the back-office report page itself is showing (a ``MainContent_`` element on
    www.eposnowhq.com); the login form, a timeout or anything else means: log in as before."""
    form = page.get_by_role("textbox", name="Username or email address")
    try:
        page.locator('[id^="MainContent_"]').or_(form).first.wait_for(timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - unknown page: log in
        return True
    if (urlparse(page.url).hostname or "") == LOGIN_HOST or form.count() > 0:
        return True
    return page.locator('[id^="MainContent_"]').count() == 0


def clear(company_key: str) -> None:
    try:
        path(company_key).unlink()
    except OSError:
        pass
