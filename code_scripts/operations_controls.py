"""Offline reconciliation, proposed-write queue and exact posting approvals.

No network access. EPOS evidence cannot imply a bank transfer or bill payment.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from code_scripts.conversion_contract import finite


def payload_digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()


def request_id(realm, kind, source_key):
    if not all((realm,kind,source_key)):
        raise ValueError('Realm, entity type and source identity required')
    return hashlib.sha256(f'{realm}:{kind}:{source_key}'.encode()).hexdigest()[:50]


def require_posting_approval(path, payload, realm, kind, now=None):
    if not path:
        raise ValueError('Company A posting requires an explicit approved batch manifest')
    doc=json.loads(Path(path).read_text())
    now=now or datetime.now(timezone.utc)
    expires=datetime.fromisoformat(doc['expires_at'])
    if expires.tzinfo is None or expires<=now:
        raise ValueError('Posting approval expired or lacks timezone')
    if doc.get('realm')!=str(realm) or doc.get('entity')!=kind or not doc.get('chat_approval_ref') or not doc.get('approved_by'):
        raise ValueError('Posting approval identity/reference missing')
    digest=payload_digest(payload)
    if digest not in doc.get('payload_sha256',[]):
        raise ValueError('Payload is not in the approved batch; re-review required')
    return digest


class ProposalQueue:
    """Local durable audit/proposal store. This class has no posting method.

    source_key must be stable: supplier+invoice number, EPOS source transaction,
    or statement account+line ID. One source cannot silently change its payload.
    """
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.path=str(path)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS proposals (
              request_id TEXT PRIMARY KEY, realm TEXT NOT NULL, kind TEXT NOT NULL,
              source_key TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'PROPOSED');
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, event TEXT NOT NULL,
              detail TEXT NOT NULL, at TEXT NOT NULL);
            ''')

    def connect(self):
        return sqlite3.connect(self.path,timeout=30)

    def propose(self,realm,kind,source_key,payload,evidence):
        """Record (or re-open) the proposal for one stable source identity.

        - Same payload again: idempotent (a FAILED proposal is re-opened for retry).
        - Different payload: allowed only when the earlier attempt is confirmed FAILED
          (QBO rejected it, so nothing was posted). PROPOSED/UNKNOWN outcomes must be
          reconciled in QBO first and then reset with ``mark-failed``. POSTED never changes.
        """
        if not evidence:
            raise ValueError('Source evidence required')
        rid=request_id(realm,kind,source_key);digest=payload_digest(payload)
        now=datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT digest,state FROM proposals WHERE request_id=?',(rid,)).fetchone()
            if old and old[1]=='POSTED' and old[0]!=digest:
                raise ValueError('Source identity already posted with a different payload')
            if old and old[0]!=digest and old[1]!='FAILED':
                raise ValueError('Source identity already has a different payload with an unresolved outcome ('
                                 +old[1]+'); reconcile QBO, then run operations_controls mark-failed before retrying')
            if not old:
                db.execute('INSERT INTO proposals(request_id,realm,kind,source_key,digest,payload) VALUES(?,?,?,?,?,?)',
                           (rid,realm,kind,source_key,digest,json.dumps(payload,sort_keys=True)))
                db.execute('INSERT INTO audit(request_id,event,detail,at) VALUES(?,?,?,?)',
                           (rid,'PROPOSED',json.dumps(evidence,sort_keys=True),now))
            elif old[1]=='FAILED':
                db.execute("UPDATE proposals SET digest=?,payload=?,state='PROPOSED' WHERE request_id=?",
                           (digest,json.dumps(payload,sort_keys=True),rid))
                db.execute('INSERT INTO audit(request_id,event,detail,at) VALUES(?,?,?,?)',
                           (rid,'REPROPOSED' if old[0]!=digest else 'RETRY',
                            json.dumps({'evidence':evidence,'previous_digest':old[0]},sort_keys=True),now))
        return rid

    def http_request_id(self,rid):
        """Intuit ``requestid`` for the next POST of this proposal.

        Stable across transport retries of the same payload (an uncertain POST is
        de-duplicated by QBO), but new after each confirmed FAILED outcome or payload
        change, so QBO cannot replay a cached failure for a corrected retry.
        """
        with self.connect() as db:
            row=db.execute('SELECT realm,kind,source_key,digest FROM proposals WHERE request_id=?',(rid,)).fetchone()
            if row is None:
                raise ValueError('Unknown proposal')
            failures=db.execute("SELECT count(*) FROM audit WHERE request_id=? AND event='FAILED'",(rid,)).fetchone()[0]
        realm,kind,source_key,digest=row
        return request_id(realm,kind,f'{source_key}:{digest}:attempt{failures}')

    def state(self,rid):
        with self.connect() as db:
            row=db.execute('SELECT state FROM proposals WHERE request_id=?',(rid,)).fetchone()
        return row[0] if row else None

    def mark_failed(self,rid,*,reason,approved_by):
        """Operator reset after confirming in QBO that the receipt does NOT exist."""
        if not reason or not approved_by:
            raise ValueError('Reason and approver required to mark a proposal failed')
        self.record_result(rid,'FAILED',detail=f'manual: {reason} (by {approved_by})')

    def record_result(self, rid, state, *, qbo_id="", detail=""):
        if state not in {"POSTED", "FAILED", "UNKNOWN"}:
            raise ValueError("Invalid posting outcome")
        if state == "POSTED" and not qbo_id:
            raise ValueError("Confirmed QBO Id required")
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT state FROM proposals WHERE request_id=?', (rid,)).fetchone()
            if old is None:
                raise ValueError("Unknown proposal")
            if old[0] == "POSTED" and state != "POSTED":
                raise ValueError("Posted outcome cannot be downgraded")
            db.execute('UPDATE proposals SET state=? WHERE request_id=?', (state,rid))
            db.execute('INSERT INTO audit(request_id,event,detail,at) VALUES(?,?,?,?)',
                       (rid,state,json.dumps({"qbo_id":qbo_id,"detail":detail}),datetime.now(timezone.utc).isoformat()))


def reconcile_documents(source, qbo):
    """Compare stable keys and gross/net/VAT, never just DocNumber presence."""
    def index(rows):
        grouped={}
        for row in rows:grouped.setdefault(row['key'],[]).append(row)
        return grouped
    a,b=index(source),index(qbo);issues=[]
    for key in sorted(a.keys()|b.keys()):
        if len(a.get(key,[]))!=1 or len(b.get(key,[]))!=1:
            issues.append({'key':key,'reason':'MISSING_OR_DUPLICATE','source_count':len(a.get(key,[])),'qbo_count':len(b.get(key,[]))});continue
        for field in ('gross','net','vat'):
            delta=finite(a[key][0][field])-finite(b[key][0][field])
            if abs(delta)>Decimal('0.01'):issues.append({'key':key,'reason':field.upper()+'_MISMATCH','delta':str(delta)})
    return {'source_count':len(source),'qbo_count':len(qbo),'issues':issues,'matched':not issues}


def purchase_units(packs, units_per_pack, cost_per_pack):
    qty,factor,cost=map(finite,(packs,units_per_pack,cost_per_pack))
    if qty<=0 or factor<=0 or cost<0:raise ValueError('Invalid purchase unit evidence')
    return {'quantity':qty*factor,'unit_cost':cost/factor,'amount':qty*cost}


def settlement_match(gross,fees,refunds,bank_amount,*,statement_reference):
    if not statement_reference:raise ValueError('Independent bank statement reference required')
    expected=finite(gross)-finite(fees)-finite(refunds)
    delta=expected-finite(bank_amount)
    return {'expected_bank':str(expected),'difference':str(delta),'matched':abs(delta)<=Decimal('0.01'),
            'action':'RECONCILE_ONLY_NO_BANK_POSTING'}


def create_night_offset(ia_before, verified_create_increase, final_epos_value):
    before,increase,target=map(finite,(ia_before,verified_create_increase,final_epos_value))
    if increase<0 or target<0:raise ValueError('Opening value must be non-negative')
    credit=before+increase-target
    return {'ia_before':str(before),'create_increase':str(increase),'target':str(target),
            'credit_inventory_77':str(credit),'debit_equity_86':str(credit),'ia_after':str(target),
            'status':'PROPOSED_REQUIRES_CHAT_APPROVAL; negative amount reverses sides'}


def close_control(period, opening, closing, *, purchases_already_expensed):
    if period>='2026-10':
        raise ValueError('October onwards: perpetual FIFO COGS is primary; verified variance only')
    if not purchases_already_expensed:
        raise ValueError('Use separately verified purchases; stock-movement-only journal is not applicable')
    delta=finite(closing)-finite(opening)
    return {'debit_inventory_77':str(delta),'credit_cogs_76':str(delta),'status':'PROPOSED_ONLY'}


def receipt_signature(payload):
    """Financial + item identity signature; ignore API metadata/order/line IDs."""
    lines={}
    for line in payload.get('Line',[]):
        if line.get('DetailType')!='SalesItemLineDetail':
            continue
        detail=line['SalesItemLineDetail'];key=str(detail['ItemRef']['value'])
        pair=lines.setdefault(key,[Decimal(0),Decimal(0)])
        pair[0]+=finite(detail.get('Qty',1));pair[1]+=finite(line['Amount'])
    return {'date':payload.get('TxnDate'),'doc':payload.get('DocNumber'),
            'deposit':str((payload.get('DepositToAccountRef') or {}).get('value','')),
            'payment':str((payload.get('PaymentMethodRef') or {}).get('value','')),
            'memo':payload.get('PrivateNote',''),
            'lines':lines,'tax':finite((payload.get('TxnTaxDetail') or {}).get('TotalTax',0))}


def assert_receipt_matches(expected,actual):
    a,b=receipt_signature(expected),receipt_signature(actual)
    for field in ('date','doc','deposit','payment','memo'):
        if a[field]!=b[field]:raise ValueError('Existing QBO receipt '+field+' mismatch')
    if a['lines'].keys()!=b['lines'].keys():raise ValueError('Existing QBO receipt item identity mismatch')
    for key in a['lines']:
        if abs(a['lines'][key][0]-b['lines'][key][0])>Decimal('0.00000001') or abs(a['lines'][key][1]-b['lines'][key][1])>Decimal('0.01'):
            raise ValueError('Existing QBO receipt quantity/amount mismatch')
    if abs(a['tax']-b['tax'])>Decimal('0.01'):raise ValueError('Existing QBO receipt VAT mismatch')


def posting_hold_path():
    from code_scripts.paths import STATE_ROOT
    return STATE_ROOT/'company_a_posting_hold.json'


def _as_date(value):
    from datetime import date
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def is_controlled_business_date(value, fail_closed_from=None):
    """True when a Company A business date is on/after the Inventory cutover.

    The cutover is ``min(fail_closed_from, 2026-10-01)``; it can never move later.
    Missing or unparseable dates are treated as controlled (fail closed).
    """
    from code_scripts.conversion_contract import CUTOVER
    cutover = CUTOVER
    if fail_closed_from is not None:
        try:
            cutover = min(_as_date(fail_closed_from), CUTOVER)
        except (TypeError, ValueError):
            cutover = CUTOVER
    try:
        return _as_date(value) >= cutover
    except (TypeError, ValueError):
        return True


HOLD_CLEAR_HINT = ('clear with `python -m code_scripts.operations_controls clear-hold '
                   '--approved-by NAME --reason TEXT`')


def assert_no_posting_hold():
    if posting_hold_path().exists():
        raise ValueError('Company A posting is on reconciliation hold; resolve, then ' + HOLD_CLEAR_HINT)


def require_reconciliation_match(company_key, result, business_date=None):
    """Write the posting hold for a failed/non-MATCH Company A result on/after cutover.

    Pre-October (catch-all history) days never write the hold; they fail or warn
    exactly as before. ``business_date=None`` is treated as controlled (fail closed).
    """
    if company_key!='company_a' or (result or {}).get('status')=='MATCH':return
    if business_date is not None and not is_controlled_business_date(business_date):return
    path=posting_hold_path();path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'business_date':str(business_date or ''),
        'result':result,
        'action':'PAUSE; reconcile QBO and source evidence; human approval before clearing hold'},indent=2))
    raise RuntimeError('Company A reconciliation failed/not run; posting hold written, subsequent October uploads '
                       'blocked; ' + HOLD_CLEAR_HINT)


def clear_posting_hold(*, approved_by, reason, now=None):
    """Archive (never delete) the hold file with who/why. Returns the archive path or None."""
    if not approved_by or not reason:
        raise ValueError('Approver and reason required to clear the posting hold')
    path=posting_hold_path()
    if not path.exists():
        return None
    now=now or datetime.now(timezone.utc)
    text=path.read_text()
    try:
        doc=json.loads(text)
    except ValueError:
        doc={'raw':text}
    doc['cleared']={'at':now.isoformat(),'approved_by':approved_by,'reason':reason}
    archive=path.with_name(f"company_a_posting_hold.cleared-{now.strftime('%Y%m%dT%H%M%S%fZ')}.json")
    archive.write_text(json.dumps(doc,indent=2))
    path.unlink()
    return archive


def build_posting_manifest(evidence, *, realm, approved_by, chat_approval_ref, expires_at, entity='SalesReceipt'):
    """Build an exact-payload approval manifest from a ``qbo_upload --dry-run`` evidence document.

    Only payloads flagged ``requires_approval`` (TxnDate on/after cutover) are included.
    Each digest is recomputed from its payload, never trusted from the file.
    """
    if not approved_by or not chat_approval_ref:
        raise ValueError('Approver and chat approval reference required')
    expires=datetime.fromisoformat(str(expires_at))
    if expires.tzinfo is None:
        raise ValueError('expires_at must include a timezone')
    if isinstance(evidence,dict):
        if str(evidence.get('realm') or realm)!=str(realm):
            raise ValueError('Evidence realm differs from manifest realm')
        if evidence.get('complete') is False:
            raise ValueError('Evidence is incomplete (some receipts failed preflight); fix and re-run the dry-run')
        entries=evidence.get('payloads') or []
    else:
        entries=evidence or []
    digests=[]
    for entry in entries:
        if entry.get('requires_approval',True) is False:
            continue
        digest=payload_digest(entry['payload'])
        if entry.get('sha256') and entry['sha256']!=digest:
            raise ValueError('Evidence digest does not match its payload')
        digests.append(digest)
    if not digests:
        raise ValueError('No approval-requiring payloads in evidence')
    return {'realm':str(realm),'entity':entity,'approved_by':approved_by,'chat_approval_ref':chat_approval_ref,
            'expires_at':expires.isoformat(),'payload_sha256':sorted(set(digests))}


def _cli(argv=None):
    import argparse
    from datetime import timedelta
    parser=argparse.ArgumentParser(prog='python -m code_scripts.operations_controls',
        description='Company A posting controls: hold, approval manifest, proposal reset. No QBO calls.')
    sub=parser.add_subparsers(dest='cmd',required=True)
    sub.add_parser('show-hold',help='Print the current posting hold, if any')
    ch=sub.add_parser('clear-hold',help='Archive the posting hold after reconciliation (human approval)')
    ch.add_argument('--approved-by',required=True);ch.add_argument('--reason',required=True)
    mm=sub.add_parser('make-manifest',help='Build an approval manifest from a qbo_upload --dry-run evidence file')
    mm.add_argument('--evidence',required=True);mm.add_argument('--out',required=True)
    mm.add_argument('--approved-by',required=True);mm.add_argument('--chat-ref',required=True)
    mm.add_argument('--realm',default='9341455406194328')
    mm.add_argument('--expires-hours',type=float,default=24.0)
    mf=sub.add_parser('mark-failed',help='Reset an UNKNOWN/PROPOSED receipt after confirming it is NOT in QBO')
    mf.add_argument('--doc-number',required=True);mf.add_argument('--txn-date',required=True)
    mf.add_argument('--approved-by',required=True);mf.add_argument('--reason',required=True)
    mf.add_argument('--realm',default='9341455406194328')
    args=parser.parse_args(argv)
    if args.cmd=='show-hold':
        path=posting_hold_path()
        print(path.read_text() if path.exists() else f'No posting hold at {path}')
        return 0
    if args.cmd=='clear-hold':
        archived=clear_posting_hold(approved_by=args.approved_by,reason=args.reason)
        print(f'Posting hold archived to {archived}' if archived else f'No posting hold at {posting_hold_path()}')
        return 0
    if args.cmd=='make-manifest':
        evidence=json.loads(Path(args.evidence).read_text())
        expires=datetime.now(timezone.utc)+timedelta(hours=args.expires_hours)
        manifest=build_posting_manifest(evidence,realm=args.realm,approved_by=args.approved_by,
            chat_approval_ref=args.chat_ref,expires_at=expires.isoformat())
        out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(manifest,indent=2))
        print(f"Wrote manifest with {len(manifest['payload_sha256'])} payload digest(s) to {out}; "
              f'export COMPANY_A_POSTING_APPROVAL_FILE={out}')
        return 0
    if args.cmd=='mark-failed':
        from code_scripts.paths import STATE_ROOT
        queue=ProposalQueue(STATE_ROOT/'company_a_proposals.sqlite')
        rid=request_id(args.realm,'SalesReceipt',f'{args.doc_number}:{args.txn_date}')
        if queue.state(rid) is None:
            print('No proposal found for that DocNumber/TxnDate');return 1
        queue.mark_failed(rid,reason=args.reason,approved_by=args.approved_by)
        print(f'Proposal {args.doc_number}:{args.txn_date} marked FAILED; the next run may retry it')
        return 0
    return 2


if __name__=='__main__':
    raise SystemExit(_cli())
