"""Read-only deposit register over pipeline day state and plans."""
import os,re
from datetime import date,timedelta
from django.core.paginator import Paginator
from django.urls import reverse
from . import workspace_records as r

LABELS={'DEPOSITED':('Banked','success'),'READY':('Ready to bank','neutral'),'HELD':('Needs attention','danger'),'WAITING_SHEET':('Waiting for till sheet','neutral'),'NO_SALES':('No sales posted yet','neutral')}


def reason(text):
    low=str(text).lower()
    if 'over the tolerance' in low:
        match=re.search(r'sheet total (.+?) vs receipts (.+?): difference (.+?) is over the tolerance (.+)',str(text))
        if match:return f'Till sheet total ({match[1]}) and sales ({match[2]}) differ by {match[3]}, more than the allowed {match[4]}. Check the sheet or sales for this day.'
    if 'unknown till line' in low:return "The till sheet has a line we don't recognise. Add it to the till accounts list."
    if 'closed period' in low:return 'This day is in a closed period in QuickBooks. Review the closing date before continuing.'
    if 'qbo account' in low or 'bank that is not in' in low:return "A bank account for this day isn't set up correctly in QuickBooks. Check the till accounts list and bank record."
    if 'already deposited outside' in low:return "Some of this day's sales were banked by hand. Check before banking the rest."
    if 'over the automatic cap' in low:return "This day is above the automatic limit. Review the amounts before approving it."
    if 'post stopped' in low or 'post /deposit failed' in low or 'post /transfer failed' in low:return 'Banking this day stopped part-way. Open details before retrying; completed deposits will not be repeated.'
    if 'no salesreceipts' in low:return "Sales for this day aren't in QuickBooks yet."
    if 'box is blank' in low:return str(text).split(' box is blank')[0]+': enter the amount on the till sheet, or type 0 if there was none.'
    if 'not found' in low or 'sheet day is blank' in low:return "The till sheet for this day isn't filled in yet."
    return 'Review the supporting details before continuing.'


def settings():
    keys={'OIAT_COMPANY_A_UF_TOLERANCE':'1000','OIAT_COMPANY_A_UF_TOLERANCE_PCT':'0.5'}
    values={k:os.environ.get(k,default) for k,default in keys.items()}
    path=r.ops.ops_root()/'uf_deposits/settings.env'
    if path.exists():
        for line in r.safe(path).read_text().splitlines():
            if '=' in line:
                k,v=line.split('=',1)
                if k.strip() in keys:values[k.strip()]=v.split(' #',1)[0].strip().strip('\"\'')
    return {k:r.number(v) for k,v in values.items()}


def plan_folders():
    candidates=[]
    for run in r.ops.list_runs(limit=200):
        if run.dry_run:continue
        for name in ('uf','deposits','uf_deposits'):
            folder=run.path/name
            if folder.is_dir():candidates += [p for p in folder.glob('*/summary.json') if r.ops.DATE_RE.fullmatch(p.parent.name)]
    root=r.read_root()
    if root.exists():candidates += list(root.glob('*/deposits/*/summary.json'))[:200]
    return sorted(candidates,key=lambda p:p.stat().st_mtime,reverse=True)


def context(request):
    errors=[];state={};report={};balance=None;checked=None
    path=r.ops.ops_root()/'uf_deposits/days.json'
    try:
        state=r.document(path).get('days',{}) if path.exists() else {}
        if not isinstance(state,dict):raise ValueError('Invalid days')
    except (OSError,ValueError,TypeError):state={};errors.append('Deposit day records could not be read.')
    reports=[]
    for run in r.ops.list_runs(limit=50):
        for name in ('uf','deposits','uf_deposits'):
            p=run.path/name/'summary.json'
            if p.exists():
                try:
                    doc=r.document(p);reports.append((p.stat().st_mtime,doc))
                except (OSError,ValueError):errors.append('Some deposit check records could not be read.')
        step=next((s for s in run.steps if s.name in {'uf','uf_deposits','deposits'}),None)
        if balance is None and step and r.number(step.counts.get('uf_balance')) is not None:
            balance=step.counts['uf_balance'];checked=run.finished_at
    status_path=r.read_root()/'latest_deposit_status.json'
    if status_path.exists():
        try:reports.append((status_path.stat().st_mtime,r.document(status_path)))
        except (OSError,ValueError):errors.append('The latest till-sheet status could not be read.')
    for _,doc in sorted(reports,reverse=True,key=lambda x:x[0]):
        if not report and isinstance(doc.get('till_sheet'),dict):report=doc['till_sheet']
        if balance is None and r.number(doc.get('uf_balance')) is not None:balance=doc['uf_balance'];checked=r.timestamp(doc.get('finished_at') or doc.get('at'))
    plans={}
    for p in plan_folders():
        try:
            doc=r.document(p);day=doc.get('day')
            if day and r.ops.DATE_RE.fullmatch(day) and day not in plans:plans[day]=(doc,p.parent)
        except (OSError,ValueError,TypeError):errors.append('Some deposit plans could not be read.')
    try:tol=settings()
    except (OSError,ValueError):tol={};errors.append('Deposit tolerance could not be read.')
    from code_scripts.akponora_ops.daily_run import last_closed_business_date
    end=last_closed_business_date();start=date(2026,9,25);rows=[]
    missing=set(report.get('missing') or []);incomplete={x.get('day') for x in report.get('incomplete') or [] if isinstance(x,dict)}
    while start<=end and len(rows)<1000:
        day=start.isoformat();entry=state.get(day) or {};doc,folder=plans.get(day,({},None))
        st=entry.get('status') or doc.get('state') or ('WAITING_SHEET' if day in missing|incomplete else 'UNKNOWN')
        if entry.get('status')!='DEPOSITED' and folder and (not r.timestamp(entry.get('updated_at')) or folder.joinpath('summary.json').stat().st_mtime>r.timestamp(entry.get('updated_at')).timestamp()):st=doc.get('state') or st
        if any(r.document(p).get('complete') for p in folder.glob('post_*.json')) if folder else False:st='DEPOSITED'
        sales=r.number(doc.get('receipts_total') if doc.get('receipts_total') is not None else entry.get('receipts_total'));sheet=r.number(doc.get('sheet_total'))
        diff=sheet-sales if sheet is not None and sales is not None else None
        abs_tol=tol.get('OIAT_COMPANY_A_UF_TOLERANCE');pct=tol.get('OIAT_COMPANY_A_UF_TOLERANCE_PCT')
        allowed=max(abs_tol,sales*pct/100) if None not in (abs_tol,pct,sales) else None
        label,tone=LABELS.get(st,('Not checked yet','neutral'))
        raw=entry.get('reason') or '; '.join(doc.get('reasons') or [])
        message={'DEPOSITED':'Moved to the banks.','READY':'Till sheet and sales agree. Ready to move to the banks.','WAITING_SHEET':"The till sheet for this day isn't filled in yet.",'NO_SALES':"Sales for this day aren't in QuickBooks yet."}.get(st,reason(raw) if raw else 'This day has not been checked yet.')
        if st=='WAITING_SHEET' and raw:message=reason(raw)
        bank_names={}
        account_file=r.ops.state_root()/'mappings/company_a/till_accounts.csv'
        # Read below once; per-bank amounts stay in the pipeline's units.
        rows.append(dict(day=start,date=day,status=st,label=label,tone=tone,message=message,
            sales=r.money(sales),sheet=r.money(sheet) if sheet is not None else 'Incomplete' if day in incomplete else 'Not entered',
            difference=r.money(diff) if diff is not None else 'Not compared',outside=bool(diff is not None and allowed is not None and abs(diff)>allowed),
            summary=doc,folder=folder,updated=r.timestamp(entry.get('updated_at')),url='?tab=deposits&day='+day+'#deposit-details'))
        start+=timedelta(days=1)
    rows.reverse();selected=next((row for row in rows if row['date']==request.GET.get('day')),None)
    bank_names={};account_rows=[]
    try:
        file=r.ops.state_root()/'mappings/company_a/till_accounts.csv'
        account_rows=r.rows(file) if file.exists() else []
        for account in account_rows:
            number=account.get('QBO account number','');name=account.get('Till sheet line') or number
            terminal=account.get('Terminal / TID')
            bank_names.setdefault(number,[]).append(name+(f' ({terminal})' if terminal else ''))
    except (OSError,ValueError):errors.append('Till account names could not be read.')
    banks=[]
    if selected and selected['folder']:
        try:
            for bank in r.rows(selected['folder']/'review.csv'):
                banks.append(dict(name=' / '.join(dict.fromkeys(bank_names.get(bank.get('Bank No'),[bank.get('Bank No') or 'Unnamed bank']))),sheet=r.money(bank.get('Sheet Amount')),target=r.money(bank.get('Target')),final=r.money(bank.get('Final')),status=LABELS.get(selected['status'],('Needs checking','neutral'))[0]))
        except (OSError,ValueError):errors.append('Bank details could not be read.')
    from . import attention
    items,_=attention.inbox()
    decisions={i['identity']:i for i in items if i['kind']=='deposit'}
    for row in rows:
        row['decision']=decisions.get(row['date'])
        row['approve']=bool(row['status']=='READY' and row['decision'] and row['decision']['approve'])
    sheet_id=os.getenv('OIAT_COMPANY_A_TILL_SHEET_ID','').strip()
    if not sheet_id:
        from code_scripts.akponora_ops.till_sheet import DEFAULT_SHEET_ID
        sheet_id=DEFAULT_SHEET_ID
    sheet_url='https://docs.google.com/spreadsheets/d/'+sheet_id if re.fullmatch(r'[A-Za-z0-9_-]+',sheet_id) else ''
    from ..models import RunJob
    latest=RunJob.objects.filter(scope=RunJob.SCOPE_WORKSPACE_READ,company_key='company_a',inventory_options_json__action__in=['deposit_plan','deposit_status']).order_by('-created_at').first()
    return dict(deposit_page=Paginator(rows,20).get_page(request.GET.get('page')),deposit_selected=selected,deposit_banks=banks,
        deposit_errors=list(dict.fromkeys(errors)),deposit_balance=r.money(balance),deposit_checked=checked,
        deposit_last_day=r.timestamp(report.get('last_complete_day')),deposit_last_day_date=date.fromisoformat(report['last_complete_day']) if report.get('last_complete_day') and r.ops.DATE_RE.fullmatch(report['last_complete_day']) else None,
        deposit_missing=sorted(missing),deposit_incomplete=sorted(d for d in incomplete if d),deposit_report_text=report.get('text',''),
        deposit_waiting=sum(row['status'] in {'READY','HELD'} for row in rows),deposit_sheet_url=sheet_url,
        deposit_enabled=os.getenv(r.ops.UF_ENV,'').lower() in {'1','true','yes','on'},deposit_job=latest,
        deposit_tolerance=tol,deposit_accounts=account_rows)
