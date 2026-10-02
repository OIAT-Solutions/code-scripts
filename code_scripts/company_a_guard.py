"""Hard refusals for destructive / legacy tooling against Company A (AKPONORA).

Company A went live on 1 Oct 2026 on the new AKP-/AKP-NS- items. AGENTS.md
non-negotiables: never delete QBO products or historical sales receipts, never
bulk-inactivate, no InventoryAdjustment on the sales path, and no name-based
matching onto items for October. Legacy maintenance scripts call
:func:`assert_not_company_a` (or :func:`company_a_refusal`) so they keep working
for other companies but refuse Company A by company key *or* QBO realm id.

These guards are intentionally not overridable by environment variables.
"""
from __future__ import annotations

from typing import Optional

COMPANY_A_KEY = "company_a"
COMPANY_A_REALM_ID = "9341455406194328"
COMPANY_A_GO_LIVE_DATE = "2026-10-01"


class CompanyAProtectedError(RuntimeError):
    """Raised when a destructive/legacy operation targets Company A."""


def is_company_a(company_key: Optional[str] = None, realm_id: Optional[str] = None) -> bool:
    key = str(company_key or "").strip().lower()
    realm = str(realm_id or "").strip()
    return key == COMPANY_A_KEY or realm == COMPANY_A_REALM_ID


def company_a_refusal_message(action: str, *, reason: str = "", alternative: str = "") -> str:
    msg = (
        f"Refusing {action} for Company A (company_a / AKPONORA, realm {COMPANY_A_REALM_ID}). "
        "See AGENTS.md (Company A non-negotiables)."
    )
    if reason:
        msg += f" {reason}"
    if alternative:
        msg += f" Use instead: {alternative}"
    return msg


def company_a_refusal(
    action: str,
    *,
    company_key: Optional[str] = None,
    realm_id: Optional[str] = None,
    reason: str = "",
    alternative: str = "",
) -> Optional[str]:
    """Return the refusal message when the target is Company A, else ``None``."""
    if not is_company_a(company_key, realm_id):
        return None
    return company_a_refusal_message(action, reason=reason, alternative=alternative)


def assert_not_company_a(
    action: str,
    *,
    company_key: Optional[str] = None,
    realm_id: Optional[str] = None,
    reason: str = "",
    alternative: str = "",
) -> None:
    """Raise :class:`CompanyAProtectedError` when the target is Company A."""
    msg = company_a_refusal(
        action, company_key=company_key, realm_id=realm_id, reason=reason, alternative=alternative
    )
    if msg:
        raise CompanyAProtectedError(msg)
