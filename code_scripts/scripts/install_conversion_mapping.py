"""Validate and atomically install an operator-approved mapping; no QBO writes.

Requires a pinned source digest, approver and deployment receipt. This is an
explicit operator command, never invoked from a sales job or image rebuild.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from code_scripts.product_conversion import ProductConversionRegistry, october_target_error


def install(source, destination, expected_sha256, approval_ref):
    source, destination = Path(source), Path(destination)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256 or not approval_ref.strip():
        raise ValueError('Pinned digest and explicit approval reference required')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.csv', delete=False) as tmp:
        pending = Path(tmp.name)
        tmp.write(raw)
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        registry = ProductConversionRegistry.from_csv(pending)
        for rule in registry.rules:
            reason = october_target_error(rule.target_qbo_type, rule.target_qbo_sku,
                                          rule.target_qbo_item_id, rule.target_qbo_name)
            if (reason or not rule.target_qbo_item_id.isdigit()
                    or rule.effective_date.isoformat() != '2026-10-01'):
                raise ValueError('Installed October map requires exact new Ids (Inventory with AKP- SKU or '
                                 'NonInventory with AKP-NS- SKU) and October 1 effective date'
                                 + (f': row {rule.row_id}: {reason}' if reason else f': row {rule.row_id}'))
        versions = destination.parent / 'versions'
        versions.mkdir(exist_ok=True)
        if destination.exists():
            old = destination.read_bytes()
            old_digest = hashlib.sha256(old).hexdigest()
            (versions / f'{old_digest}.csv').write_bytes(old)
        (versions / f'{digest}.csv').write_bytes(raw)
        receipt = {'sha256': digest, 'approval_ref': approval_ref, 'rows': len(registry.rules),
                   'installed_at': datetime.now(timezone.utc).isoformat()}
        (versions / f'{digest}.json').write_text(json.dumps(receipt, indent=2)+'\n')
        os.replace(pending, destination)
        return receipt
    finally:
        pending.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--destination', required=True, type=Path)
    p.add_argument('--sha256', required=True)
    p.add_argument('--approval-ref', required=True)
    a = p.parse_args()
    print(json.dumps(install(a.source, a.destination, a.sha256, a.approval_ref), indent=2))


if __name__ == '__main__':
    main()
