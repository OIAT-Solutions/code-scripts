"""Company A standing auto-approval for unattended October+ daily posting.

Off unless BOTH are set in the environment of ``run_pipeline``:

- ``OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1`` (the existing scheduler gate), and
- ``OIAT_COMPANY_A_STANDING_APPROVAL_REF=<owner approval reference>`` (non-empty).

Optional: ``OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS=<naira>`` caps the day's EPOS gross.

When on, ``run_pipeline`` runs ``qbo_upload --dry-run`` for each controlled
business day, evaluates the gates below against the dry-run evidence and the
split raw EPOS file, and only then writes an exact-payload manifest (the same
format ``operations_controls make-manifest`` builds) that is handed to the
posting subprocess alone. Any failed gate means nothing is posted and the
posting hold is written. No function here talks to QBO or EPOS.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from code_scripts.operations_controls import build_posting_manifest, payload_digest, posting_hold_path

AUTOMATION_FLAG_ENV = "OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED"
STANDING_REF_ENV = "OIAT_COMPANY_A_STANDING_APPROVAL_REF"
MAX_GROSS_ENV = "OIAT_COMPANY_A_AUTO_APPROVAL_MAX_GROSS"
AUTO_APPROVER = "auto:scheduler"
MANIFEST_TTL = timedelta(hours=6)
GROSS_TOLERANCE = Decimal("1.00")
COMPANY_A = "company_a"
# Evidence older than the dry-run start (minus clock slack) is from an earlier run.
EVIDENCE_CLOCK_SLACK = timedelta(seconds=5)

GATE_EVIDENCE = "evidence_complete"
GATE_TOTALS = "totals_match_epos"
GATE_HOLD = "no_posting_hold"
GATE_MAPPING = "mapping_sha_matches"
GATE_CAP = "max_gross_cap"


class StandingApprovalConfigError(ValueError):
    """The standing-approval environment is set but invalid (fails closed)."""


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def standing_approval_settings(env=None) -> Optional[dict]:
    """Return ``{'ref': str, 'max_gross': Decimal|None}`` when enabled, else None.

    Raises StandingApprovalConfigError when enabled with an unusable cap, so a
    typo never silently removes the cap.
    """
    env = os.environ if env is None else env
    ref = (env.get(STANDING_REF_ENV) or "").strip()
    if not (_truthy(env.get(AUTOMATION_FLAG_ENV)) and ref):
        return None
    raw_cap = (env.get(MAX_GROSS_ENV) or "").strip()
    cap = None
    if raw_cap:
        try:
            cap = Decimal(raw_cap.replace(",", ""))
        except InvalidOperation as exc:
            raise StandingApprovalConfigError(f"{MAX_GROSS_ENV} is not a number: {raw_cap!r}") from exc
        if not cap.is_finite() or cap <= 0:
            raise StandingApprovalConfigError(f"{MAX_GROSS_ENV} must be a positive amount: {raw_cap!r}")
    return {"ref": ref, "max_gross": cap}


def evidence_path(business_date: str, company_key: str = COMPANY_A) -> Path:
    from code_scripts import paths
    return Path(paths.STATE_ROOT) / "conversion_preflight" / f"{company_key}_sales_batch_{business_date}.json"


def _dec(value: Any) -> Decimal:
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"non-numeric amount {value!r}") from exc
    if not n.is_finite():
        raise ValueError(f"non-finite amount {value!r}")
    return n


def _sales_lines(payload: dict) -> list[dict]:
    return [line for line in payload.get("Line") or [] if line.get("DetailType") == "SalesItemLineDetail"]


def payload_gross(payload: dict) -> Decimal:
    """Tax-inclusive gross of one SalesReceipt payload (sum of TaxInclusiveAmt)."""
    total = Decimal(0)
    for line in _sales_lines(payload):
        detail = line.get("SalesItemLineDetail") or {}
        if "TaxInclusiveAmt" not in detail:
            raise ValueError(f"line without TaxInclusiveAmt on {payload.get('DocNumber')}")
        total += _dec(detail["TaxInclusiveAmt"])
    return total


def file_sha256(path) -> Optional[str]:
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _approved_targets(config) -> dict[str, Any]:
    """Approved October target rules by QBO Item Id from the installed mapping."""
    from code_scripts.product_conversion import ProductConversionRegistry
    registry = ProductConversionRegistry.from_csv(
        config.product_conversion_file,
        allow_name_fallback=getattr(config, "product_conversion_allow_name_fallback", True),
    )
    targets: dict[str, Any] = {}
    for rule in registry.rules:
        item_id = str(rule.target_qbo_item_id or "").strip()
        if item_id:
            targets[item_id] = rule
    return targets


def evaluate_gates(
    evidence: Optional[dict],
    *,
    business_date: str,
    config,
    raw_totals: Optional[dict],
    dry_run_started_at: Optional[datetime] = None,
    max_gross: Optional[Decimal] = None,
) -> dict:
    """Evaluate every gate. Returns ``{'passed': bool, 'failures': [...], 'summary': {...}}``.

    All gates are always evaluated (no short-circuit) so the hold names every problem.
    """
    from code_scripts.product_conversion import october_target_error

    failures: list[dict] = []
    summary: dict[str, Any] = {"business_date": business_date}

    def fail(gate: str, detail: str) -> None:
        failures.append({"gate": gate, "detail": detail})

    payloads: list[dict] = []
    existing: list[dict] = []

    # Gate 1: evidence complete and every line on an exact approved October target.
    if not isinstance(evidence, dict):
        fail(GATE_EVIDENCE, "dry-run evidence file missing or unreadable")
    else:
        payloads = list(evidence.get("payloads") or [])
        existing = list(evidence.get("existing_verified") or [])
        if evidence.get("complete") is not True:
            fail(GATE_EVIDENCE, "evidence complete is not true (some receipts failed preflight)")
        if evidence.get("company_key") != COMPANY_A:
            fail(GATE_EVIDENCE, f"evidence company_key is {evidence.get('company_key')!r}")
        if str(evidence.get("realm")) != str(getattr(config, "realm_id", "")):
            fail(GATE_EVIDENCE, "evidence realm differs from the company realm")
        if evidence.get("entity") != "SalesReceipt":
            fail(GATE_EVIDENCE, "evidence entity is not SalesReceipt")
        if str(evidence.get("target_date")) != business_date:
            fail(GATE_EVIDENCE, f"evidence target_date {evidence.get('target_date')!r} != {business_date}")
        if dry_run_started_at is not None:
            try:
                generated = datetime.fromisoformat(str(evidence.get("generated_at")))
                if generated.tzinfo is None or generated < dry_run_started_at - EVIDENCE_CLOCK_SLACK:
                    fail(GATE_EVIDENCE, "evidence predates this run's dry-run (stale file)")
            except ValueError:
                fail(GATE_EVIDENCE, "evidence generated_at missing or invalid")
        if not payloads and not existing:
            fail(GATE_EVIDENCE, "evidence contains no receipts")
        try:
            targets = _approved_targets(config)
        except Exception as exc:  # mapping unreadable/invalid: fail closed
            targets = None
            fail(GATE_EVIDENCE, f"installed mapping cannot be loaded: {exc}")
        bad_lines: list[str] = []
        for entry, to_post in [(e, True) for e in payloads] + [(e, False) for e in existing]:
            payload = entry.get("payload") or {}
            doc = entry.get("doc_number") or payload.get("DocNumber")
            if to_post and entry.get("requires_approval") is not True:
                fail(GATE_EVIDENCE, f"{doc}: payload not flagged requires_approval")
            if entry.get("sha256") != payload_digest(payload):
                fail(GATE_EVIDENCE, f"{doc}: evidence digest does not match its payload")
            if str(payload.get("TxnDate")) != business_date:
                fail(GATE_EVIDENCE, f"{doc}: TxnDate {payload.get('TxnDate')!r} != {business_date}")
            lines = _sales_lines(payload)
            if not lines:
                fail(GATE_EVIDENCE, f"{doc}: receipt has no sales lines")
            for line in lines:
                detail = line.get("SalesItemLineDetail") or {}
                item_id = str((detail.get("ItemRef") or {}).get("value") or "").strip()
                rule = (targets or {}).get(item_id)
                if targets is not None and rule is None:
                    bad_lines.append(f"{doc}: ItemRef {item_id or '?'} is not an approved mapping target")
                elif rule is not None:
                    reason = october_target_error(rule.target_qbo_type, rule.target_qbo_sku, item_id,
                                                  rule.target_qbo_name)
                    if reason:
                        bad_lines.append(f"{doc}: ItemRef {item_id}: {reason}")
                try:
                    if _dec(detail.get("Qty")) <= 0:
                        bad_lines.append(f"{doc}: ItemRef {item_id} has non-positive Qty")
                except ValueError:
                    bad_lines.append(f"{doc}: ItemRef {item_id} has invalid Qty")
        if bad_lines:
            shown = "; ".join(bad_lines[:10])
            more = f" (+{len(bad_lines) - 10} more)" if len(bad_lines) > 10 else ""
            fail(GATE_EVIDENCE, f"{len(bad_lines)} line(s) not on an approved October target: {shown}{more}")

    # Gate 2: payload gross == EPOS raw gross (₦1), receipt/line counts consistent.
    pending_gross = existing_gross = None
    try:
        pending_gross = sum((payload_gross(e.get("payload") or {}) for e in payloads), Decimal(0))
        existing_gross = sum((payload_gross(e.get("payload") or {}) for e in existing), Decimal(0))
    except ValueError as exc:
        fail(GATE_TOTALS, f"cannot total payloads: {exc}")
    raw_total = None
    if not raw_totals or raw_totals.get("total") is None:
        fail(GATE_TOTALS, "EPOS raw gross for the business day could not be computed")
    else:
        raw_total = _dec(raw_totals["total"]).quantize(Decimal("0.01"))
    if pending_gross is not None and raw_total is not None:
        day_gross = pending_gross + existing_gross
        diff = abs(day_gross - raw_total)
        if diff > GROSS_TOLERANCE:
            fail(GATE_TOTALS, f"payload gross ₦{day_gross:,.2f} (to post ₦{pending_gross:,.2f} + already in "
                              f"QBO ₦{existing_gross:,.2f}) != EPOS raw gross ₦{raw_total:,.2f} "
                              f"(difference ₦{diff:,.2f}, tolerance ₦{GROSS_TOLERANCE})")
    if isinstance(evidence, dict):
        receipts = len(payloads) + len(existing)
        if evidence.get("source_receipts") is None or int(evidence["source_receipts"]) != receipts:
            fail(GATE_TOTALS, f"receipt count mismatch: evidence has {receipts}, transformed CSV has "
                              f"{evidence.get('source_receipts')}")
        lines = sum(len(_sales_lines(e.get("payload") or {})) for e in payloads + existing)
        if evidence.get("source_rows") is None or int(evidence["source_rows"]) != lines:
            fail(GATE_TOTALS, f"line count mismatch: payloads have {lines} line(s), transformed CSV has "
                              f"{evidence.get('source_rows')} row(s)")
    summary.update({
        "receipts_to_post": len(payloads),
        "receipts_already_in_qbo": len(existing),
        "gross_to_post": str(pending_gross) if pending_gross is not None else None,
        "gross_already_in_qbo": str(existing_gross) if existing_gross is not None else None,
        "epos_raw_gross": str(raw_total) if raw_total is not None else None,
    })

    # Gate 3: no posting hold.
    hold = posting_hold_path()
    if hold.exists():
        fail(GATE_HOLD, f"posting hold present at {hold}")

    # Gate 4: mapping SHA in evidence == installed mapping SHA (and every line proof).
    installed = file_sha256(getattr(config, "product_conversion_file", None))
    summary["mapping_sha256"] = installed
    if not installed:
        fail(GATE_MAPPING, "installed mapping file cannot be read")
    elif not isinstance(evidence, dict) or evidence.get("mapping_sha256") != installed:
        fail(GATE_MAPPING, f"evidence mapping SHA {(evidence or {}).get('mapping_sha256')!r} != installed {installed}")
    elif list(evidence.get("proof_mapping_sha256") or []) != [installed]:
        fail(GATE_MAPPING, f"transformed CSV proofs use mapping SHA(s) {evidence.get('proof_mapping_sha256')!r}, "
                           f"installed {installed}")

    # Gate 5: optional cap on the business day's EPOS gross.
    if max_gross is not None:
        summary["max_gross"] = str(max_gross)
        if raw_total is None:
            fail(GATE_CAP, "cap set but EPOS raw gross unknown")
        elif raw_total > max_gross:
            fail(GATE_CAP, f"EPOS raw gross ₦{raw_total:,.2f} exceeds cap ₦{max_gross:,.2f}")

    return {"passed": not failures, "failures": failures, "summary": summary}


def format_failures(failures: list[dict]) -> str:
    return "; ".join(f"[{f['gate']}] {f['detail']}" for f in failures)


def write_auto_manifest(evidence: dict, *, ref: str, realm: str, business_date: str, now=None) -> Optional[Path]:
    """Write the auto manifest under STATE_ROOT/approvals/ (0600). None when nothing needs approval."""
    pending = [e for e in evidence.get("payloads") or [] if e.get("requires_approval", True) is not False]
    if not pending:
        return None
    now = now or datetime.now(timezone.utc)
    manifest = build_posting_manifest(
        evidence, realm=realm, approved_by=AUTO_APPROVER, chat_approval_ref=ref,
        expires_at=(now + MANIFEST_TTL).isoformat(),
    )
    manifest["business_date"] = business_date
    manifest["generated_at"] = now.isoformat()
    manifest["mode"] = "standing_auto_approval"
    from code_scripts import paths
    directory = Path(paths.STATE_ROOT) / "approvals"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = directory / f"company_a_auto_{business_date}_{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(manifest, indent=2))
    os.chmod(path, 0o600)
    return path
