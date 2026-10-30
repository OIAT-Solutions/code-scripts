"""Bounded, owner-approved recurring purchases. No network or financial writes."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


def number(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value > 0 else None
    except (InvalidOperation, ValueError):
        return None


def load_policy(path: Path):
    if not path.exists():
        return None
    policy = json.loads(path.read_text())
    if (policy.get('version') != 1 or policy.get('company') != 'company_a'
            or not str(policy.get('approval_ref') or '').strip()
            or not number(policy.get('max_bill_total'))
            or not 2 <= policy.get('minimum_prior_days', 0) <= 30
            or not 1 <= policy.get('lookback_days', 0) <= 60):
        raise ValueError('invalid purchase-pattern approval or bounds')
    seen = set()
    for row in policy.get('products', []):
        pid = row.get('product_id')
        if (not isinstance(pid, str) or not pid.isdigit() or pid in seen
                or not number(row.get('max_quantity')) or not number(row.get('unit_inc'))
                or not row.get('vendor_ids')
                or any(not isinstance(v, str) or not v.isdigit() for v in row['vendor_ids'])):
            raise ValueError('invalid or duplicate product in purchase-pattern policy')
        seen.add(pid)
    if not seen:
        raise ValueError('purchase-pattern policy has no approved products')
    return policy


def signature(po):
    result = {}
    for line in po.get('lines', []):
        qty = number(line.get('qty_received'))
        if qty:
            pid = str(line['product_id'])
            result[pid] = result.get(pid, Decimal(0)) + qty
    return result


def expected_repeat(po, history, *, vendor_id, vendor_for, policy):
    """A known recurring order; no inference of seller or independent delivery proof.

    Historical support uses only earlier receipt DATES, deduplicated by PO and day.
    Shared GRNs and same-day identical baskets remain conflicts. Current items,
    quantities, prices, seller and whole-bill value must fit explicit approval.
    """
    if not policy or not vendor_id:
        return None
    total = number(po.get('total_inc'))
    if total is None or total > Decimal(str(policy['max_bill_total'])):
        return None
    refs = {s.strip() for s in str(po.get('grn') or '').split(';') if s.strip()}
    if not refs:
        return None
    peers = {p['ref']: p for p in history if p['ref'] != po['ref'] and p.get('has_detail')}
    current = signature(po)
    if not current:
        return None
    for other in peers.values():
        if refs & {s.strip() for s in str(other.get('grn') or '').split(';') if s.strip()}:
            return None
        if (other.get('received_date') == po['received_date']
                and vendor_for(other) == vendor_id and signature(other) == current):
            return None
    profiles = {r['product_id']: r for r in policy['products']}
    evidence = []
    for pid, qty in current.items():
        profile = profiles.get(pid)
        if (not profile or vendor_id not in profile['vendor_ids']
                or qty > Decimal(str(profile['max_quantity']))):
            return None
        price = Decimal(str(profile['unit_inc']))
        for line in po['lines']:
            if str(line['product_id']) == pid and number(line.get('qty_received')):
                paid = number(line.get('unit_inc'))
                if paid is None or abs(paid - price) > price * Decimal('0.10'):
                    return None
        days, prior_refs = set(), set()
        for other in peers.values():
            day = other.get('received_date') or ''
            if not day or day >= po['received_date']:
                continue
            gap = (date.fromisoformat(po['received_date']) - date.fromisoformat(day)).days
            if gap > policy['lookback_days'] or vendor_for(other) not in profile['vendor_ids']:
                continue
            if (not other.get('grn') or other.get('status', '').casefold()
                    not in {'received', 'partially received', 'partiallyreceived', 'part received'}):
                continue
            if any(str(line['product_id']) == pid and number(line.get('qty_received'))
                   and number(line.get('unit_inc')) is not None
                   and abs(Decimal(str(line['unit_inc'])) - price) <= price * Decimal('0.10')
                   for line in other['lines']):
                days.add(day)
                prior_refs.add(other['ref'])
        if len(days) < policy['minimum_prior_days']:
            return None
        evidence.append({'product_id': pid, 'prior_days': sorted(days), 'prior_pos': sorted(prior_refs)})
    return {'approval_ref': policy['approval_ref'], 'products': evidence,
            'meaning': 'fits approved recurring-purchase bounds; separate delivery is not independently certified'}
