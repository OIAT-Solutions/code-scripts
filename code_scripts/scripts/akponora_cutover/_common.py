"""Shared helpers for the Akponora / NORA (company_a) cutover tools.

Nothing in this module talks to QBO or EPOS at import time, and no
``code_scripts.*`` module is imported at module level: ``code_scripts.paths``
resolves ``OIAT_COMPANIES_DIR`` when it is first imported, so ``setup_env()``
must run before any of them.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPANY_KEY = "company_a"
# Evidence packs (EPOS exports, stock reports, BookKeeping CSVs) live next to the repo.
DEFAULT_EVIDENCE_ROOT = REPO_ROOT.parent / "MISC" / "AKPONORA Investigation" / "COGS Analysis"
# The 25-26 Sep 2026 cutover working folder (historical inputs used as defaults).
GAPS_DIR = REPO_ROOT / "outputs" / "nora_gaps_2026-09-25"
BUSINESS_DAY_CUTOFF_HOUR = 5  # EPOS business day = 05:00 Africa/Lagos to 04:59 next day
EPOS_LOGIN_URL = "https://www.eposnowhq.com/Pages/Reporting/SageReport.aspx"
BOOKKEEPING_TS_FORMAT = "%d/%m/%Y %H:%M:%S"


# ---------------------------------------------------------------- environment
def setup_env() -> None:
    """Default OIAT_COMPANIES_DIR to code_scripts/companies and load the .env file."""
    os.environ.setdefault("OIAT_COMPANIES_DIR", str(REPO_ROOT / "code_scripts" / "companies"))
    from code_scripts.load_env import load_env_file

    load_env_file()


def company_config(company_key: str = COMPANY_KEY):
    setup_env()
    from code_scripts.company_config import load_company_config

    return load_company_config(company_key)


# ---------------------------------------------------------------- paths / files
def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def resolve_out(out: str | Path | None, tool: str) -> Path:
    """--out if given, else outputs/<tool>_<timestamp>/ (created)."""
    path = Path(out) if out else REPO_ROOT / "outputs" / f"{tool}_{timestamp()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def evidence(root: str | Path | None, relative: str) -> Path:
    return Path(root or DEFAULT_EVIDENCE_ROOT) / relative


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_csv(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- business day
def business_date(ts: datetime, cutoff_hour: int = BUSINESS_DAY_CUTOFF_HOUR) -> date:
    """EPOS business date: a sale before the cutoff hour belongs to the previous day."""
    return (ts - timedelta(hours=cutoff_hour)).date()


def parse_bookkeeping_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime((value or "").strip(), BOOKKEEPING_TS_FORMAT)
    except ValueError:
        return None


# ---------------------------------------------------------------- EPOS (view only)
def epos_login(page, cfg) -> None:
    """Log in to EPOS Now back office with the pipeline credentials (never printed)."""
    page.goto(EPOS_LOGIN_URL)
    page.get_by_role("textbox", name="Username or email address").fill(cfg.epos_username)
    page.get_by_role("textbox", name="Password").fill(cfg.epos_password)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_load_state("networkidle")


# ---------------------------------------------------------------- QBO (GET only)
class ReadOnlyQBO:
    """GET-only QBO client for company_a. The access token is never printed."""

    def __init__(self, company_key: str = COMPANY_KEY, minorversion: str = "75", max_rps: float = 5.0):
        cfg = company_config(company_key)
        from code_scripts.company_config import get_qbo_api_base_url
        from code_scripts.token_manager import get_access_token

        import requests

        self._requests = requests
        self._get_token = lambda: get_access_token(cfg.company_key, cfg.realm_id)
        self._token = self._get_token()
        self.company_key = cfg.company_key
        self.realm = cfg.realm_id
        self.base = f"{get_qbo_api_base_url(cfg.qbo_environment)}/v3/company/{cfg.realm_id}"
        self.minorversion = minorversion
        self._min_interval = 1.0 / max_rps
        self._last = 0.0
        self.requests = 0

    def get(self, path: str, retries: int = 5) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}minorversion={self.minorversion}"
        delay = 2.0
        for attempt in range(retries + 1):
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            resp = self._requests.get(
                url, headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"}, timeout=120
            )
            if resp.status_code == 401 and attempt < retries:
                self._token = self._get_token()
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def query(self, sql: str) -> dict:
        return self.get(f"query?query={quote(sql)}").get("QueryResponse", {})

    def query_all(self, sql: str, entity: str, page_size: int = 1000) -> list[dict]:
        out, start = [], 1
        while True:
            rows = self.query(f"{sql} startposition {start} maxresults {page_size}").get(entity, [])
            rows = rows if isinstance(rows, list) else [rows]
            out.extend(rows)
            if len(rows) < page_size:
                return out
            start += page_size


def dump_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, default=str) + "\n", encoding="utf-8")
