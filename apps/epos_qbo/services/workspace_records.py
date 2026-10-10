"""Bounded local evidence reads. No remote calls and no file creation."""
import csv
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
from . import company_a_ops as ops

MAX_BYTES = 32 * 1024 * 1024


def safe(path):
    path = Path(path)
    if not path.resolve().is_relative_to(ops.ops_root().resolve()) and not path.resolve().is_relative_to((ops.state_root()/"mappings/company_a").resolve()):
        raise ValueError("Record is outside the company folders")
    if path.exists() and path.stat().st_size > MAX_BYTES:
        raise ValueError("Record is too large")
    return path


def document(path):
    data=json.loads(safe(path).read_text(encoding="utf-8"))
    if not isinstance(data,dict): raise ValueError("Expected an object")
    return data


def rows(path):
    with safe(path).open(encoding="utf-8-sig",newline="") as f:
        return list(csv.DictReader(f,strict=True))


def number(value):
    try:
        n=Decimal(str(value).replace(",",""))
        return n if n.is_finite() else None
    except (InvalidOperation,ValueError,TypeError):return None


def quantity(value):
    n=number(value)
    return format(n.normalize(),"f") if n is not None else "Not checked"


def money(value):
    n=number(value)
    return f"₦{n:,.2f}" if n is not None else "Not checked yet"


def timestamp(value):
    try:
        dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError,TypeError):return None


def read_root():return ops.ops_root()/"portal_reads"
