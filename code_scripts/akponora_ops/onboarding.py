"""Read-only outstanding-balance evidence for the owner's November opening review."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal

from code_scripts.akponora_ops.common import dump_json, COMPANY, REALM


def outstanding(client, destination):
    from code_scripts.akponora_ops.payments_sheet import load_qbo
    from code_scripts.akponora_ops.work_state import digest
    qbo = load_qbo(client)
    # The payment tool intentionally lists Nora's generated credit invoices only. Opening
    # evidence must also expose older/manual invoices for review, without authorizing payments.
    qbo['invoices'] = client.query_all("select * from Invoice where Balance > '0'", "Invoice")
    records = {}
    for label, entity in (('customer_invoices', 'invoices'), ('supplier_bills', 'bills')):
        records[label] = [{k: row.get(k) for k in ('Id', 'DocNumber', 'TxnDate', 'DueDate', 'CustomerRef', 'VendorRef', 'TotalAmt', 'Balance')}
                          for row in qbo[entity] if Decimal(str(row.get('Balance') or 0)) > 0]
    document = {'company': COMPANY, 'realm': REALM, 'captured_at_utc': datetime.now(timezone.utc).isoformat(),
                'status': 'owner review required', 'evidence_sha': digest(records), **records,
                'limitation': 'QBO recorded balances only. Compare unrecorded cash/bank receipts and supplier payments before accepting an opening balance.'}
    dump_json(destination, document)
    return {'customer_invoices': len(records['customer_invoices']), 'supplier_bills': len(records['supplier_bills']),
            'evidence_sha': document['evidence_sha'], 'status': document['status'], 'financial_writes': False}


def main(argv=None):
    import json
    from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(outstanding(QBOClient.for_company_a(allow_writes=False), args.out)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
