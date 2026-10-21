"""Split EPOS credit sales out of a day's raw BookKeeping file (company_a / Nora Mart).

Owner decision (Marvin, chat 5 Oct 2026): the EPOS ``Credit`` tender (on from 5 Oct) is a sale on account
to a named customer. It must not become a SalesReceipt into Undeposited Funds (no money was received, and
the till sheet never shows it). ``run_pipeline`` calls :func:`split_raw` just before the transform: the
SalesReceipt path gets every other row, and the credit rows are saved for the daily routine's ``credit``
step (``akponora_ops/credit_invoices``), which posts one QBO Invoice per customer per day.

Saved under ``STATE_ROOT/ops/company_a/credit_sales/<business day>/``:
* ``credit_raw.csv``  rows with Tender exactly ``Credit`` (customer name + EPOS customer ID on each row);
* ``mixed_raw.csv``   rows whose tender mixes Credit with something else (e.g. ``Cash/Credit``): the split
  between the parts is not in the export, so they are kept out of both paths and wait for review;
* ``split.json``      counts and totals (written on every split, also when there were no credit rows).

``OIAT_COMPANY_A_CREDIT_SPLIT=0`` turns the split off (credit rows then go into the SalesReceipts, as before).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from code_scripts import paths as _paths

COMPANY = "company_a"
ENABLED_ENV = "OIAT_COMPANY_A_CREDIT_SPLIT"
CREDIT = "credit"


def enabled(company_key: str, env=None) -> bool:
    env = os.environ if env is None else env
    return company_key == COMPANY and str(env.get(ENABLED_ENV, "1")).strip().lower() not in {"0", "false", "no", "off"}


def day_dir(business_day: str, state_root: Path | None = None) -> Path:
    return Path(state_root or _paths.STATE_ROOT) / "ops" / COMPANY / "credit_sales" / business_day


def _tender_kind(value: str) -> str:
    parts = [p.strip().lower() for p in str(value or "").split("/") if p.strip()]
    if CREDIT not in parts:
        return ""
    return "credit" if parts == [CREDIT] else "mixed"


def _total(df: pd.DataFrame) -> float:
    if "TOTAL Sales" not in df.columns:
        return 0.0
    return float(pd.to_numeric(df["TOTAL Sales"].astype(str).str.replace(",", ""), errors="coerce").fillna(0).sum())


def split_raw(raw_file: str, business_day: str, *, state_root: Path | None = None) -> tuple[str, dict]:
    """Write the non-credit rows next to ``raw_file`` and save the credit rows; return (path to use, stats).

    Returns ``raw_file`` unchanged (and saves an empty split) when the day has no credit rows."""
    raw = pd.read_csv(raw_file, dtype=str, keep_default_na=False)
    out = day_dir(business_day, state_root)
    out.mkdir(parents=True, exist_ok=True)
    kinds = raw["Tender"].map(_tender_kind) if "Tender" in raw.columns else pd.Series([""] * len(raw), index=raw.index)
    credit, mixed, rest = raw[kinds == "credit"], raw[kinds == "mixed"], raw[kinds == ""]
    stats = {"business_day": business_day, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "source": str(raw_file), "credit_rows": len(credit), "credit_total": round(_total(credit), 2),
             "mixed_rows": len(mixed), "mixed_total": round(_total(mixed), 2), "other_rows": len(rest),
             "customers": sorted({str(n).strip() for n in credit.get("Customer Full Name", pd.Series(dtype=str))
                                  if str(n).strip()})}
    for name, frame in (("credit_raw.csv", credit), ("mixed_raw.csv", mixed)):
        path = out / name
        if len(frame):
            frame.to_csv(path, index=False)
        elif path.exists():
            path.unlink()  # a re-run after EPOS was corrected must not keep stale rows
    (out / "split.json").write_text(json.dumps(stats, indent=2))
    if not len(credit) and not len(mixed):
        return raw_file, stats
    src = Path(raw_file)
    target = src.with_name(f"{src.stem}_noncredit{src.suffix}")
    rest.to_csv(target, index=False)
    stats["noncredit_file"] = str(target)
    (out / "split.json").write_text(json.dumps(stats, indent=2))
    return str(target), stats
