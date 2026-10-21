"""Shared helpers for the company_a operations jobs.

``code_scripts.paths`` and ``code_scripts.company_config`` resolve
``STATE_ROOT`` / ``OIAT_COMPANIES_DIR`` when first imported, so they are imported
inside the functions below, after ``setup_env()`` has run.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import REPO_ROOT, company_config, setup_env

COMPANY = "company_a"
REALM = "9341455406194328"
INV_START = "2026-10-01"
ASSET_ID = "77"
CATCH_ALL_ITEM_ID = "15030"
LEGACY_PREFIX = "LEGACY \u2014 "
AKP_SKU_PREFIX = "AKP-"
AKP_NS_SKU_PREFIX = "AKP-NS-"
SLACK_ENV = "OIAT_AKPONORA_OPS_SLACK_WEBHOOK_URL"
OUTPUT_ROOT_ENV = "OIAT_AKPONORA_OPS_OUTPUT_ROOT"

MAPPING_COLUMNS = [
    "Row ID", "EPOS Product ID", "EPOS Existing SKU", "EPOS Name", "Pipeline Status", "Review Status",
    "Target QBO Item Type", "Target QBO Name", "Target QBO SKU", "Target QBO Item Id",
    "Staff Approved Sale Multiplier", "Effective Date", "Approved By", "Canonical Family Key",
    "Canonical Unit", "Staff Approved Purchase Multiplier",
]


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def state_dir(tool: str) -> Path:
    """Persistent per-job state (cursors, run receipts): ``STATE_ROOT/ops/company_a/<tool>/``."""
    setup_env()
    from code_scripts.paths import STATE_ROOT

    path = STATE_ROOT / "ops" / COMPANY / tool
    path.mkdir(parents=True, exist_ok=True)
    return path


def runs_root() -> Path:
    """Where run evidence goes: ``OIAT_AKPONORA_OPS_OUTPUT_ROOT``, else ``STATE_ROOT/ops/company_a/runs``
    when STATE_ROOT is set (server / container, where the repo folder is not persistent), else ``outputs/``."""
    explicit = os.getenv(OUTPUT_ROOT_ENV, "").strip()
    if explicit:
        return Path(explicit)
    if os.getenv("STATE_ROOT", "").strip():
        return Path(os.environ["STATE_ROOT"]) / "ops" / COMPANY / "runs"
    return REPO_ROOT / "outputs"


def run_dir(tool: str, out: str | Path | None = None) -> Path:
    """Evidence folder for one run: ``--out`` or ``<runs_root>/<tool>_<UTC timestamp>/``."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    path = Path(out) if out else runs_root() / f"{tool}_{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mapping_file() -> Path:
    """The installed approved mapping for company_a (from the company config)."""
    path = company_config(COMPANY).product_conversion_file
    if path is None:
        raise RuntimeError("company_a has no product_conversion_file configured")
    return Path(path)


def read_csv(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: str | Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump_json(path: str | Path, data) -> None:
    Path(path).write_text(json.dumps(data, indent=1, default=str) + "\n", encoding="utf-8")


def send_slack(message: str) -> None:
    """Post to the ops channel webhook, else the pipeline webhook; no-op when neither is set."""
    from code_scripts.slack_notify import send_slack_success

    send_slack_success(message, webhook_url=os.getenv(SLACK_ENV) or None)
