"""Rebuild a provisional family catalogue from preserved conversion evidence.

Read-only. Candidate families are NOT approved identities. Prior name-derived
sale multipliers are displayed only as suggestions; never copied into approval.
Stock/cost conversion uses the explicit EPOS volume denominator where present.
Multiple tracked rows may count the same stock: quarantine rather than sum.
"""
import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from pathlib import Path


def clean(v):
    return ' '.join(str(v or '').split())


def number(v):
    try:
        d = Decimal(clean(v).replace(',', ''))
        return d if d.is_finite() else None
    except InvalidOperation:
        return None


def read(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write(path, rows, fields=None):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        w=csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        w.writeheader(); w.writerows(rows)


def enrich_product_ids(rows, sales):
    ids=defaultdict(set)
    for sale in sales:
        name=clean(sale.get('Product')).casefold()
        pid=clean(sale.get('ProductId') or sale.get('ProductID'))
        if re.fullmatch(r'[0-9]+\.0+',pid): pid=pid.split('.')[0]
        if name and pid: ids[name].add(pid)
    added=0;conflicts=0
    for row in rows:
        candidates=ids.get(clean(row['EPOS Name']).casefold(),set())
        current=clean(row.get('EPOS Product ID'))
        row['Sales Product ID Candidates']=' | '.join(sorted(candidates))
        if len(candidates)>1 or (current and candidates and current not in candidates):
            row['Pipeline Status']='BLOCK'
            row['Issue Codes']=clean(row.get('Issue Codes'))+'; PRODUCT_ID_CONFLICT'
            conflicts+=1
        elif len(candidates)==1 and not current:
            row['EPOS Product ID']=next(iter(candidates));added+=1
    return {'product_ids_added_from_sales':added,'product_identity_conflicts':conflicts}


def build(rows, live):
    groups=defaultdict(list)
    for row in rows:
        family=clean(row.get('Family Candidate') or row['EPOS Name'])
        groups[family.casefold()].append(row)
    live_names=defaultdict(list)
    for item in live:
        live_names[clean(item['Name']).casefold()].append(item)
    families=[]; mappings=[]; payloads=[]; collisions=[]
    names=Counter(clean(r['EPOS Name']).casefold() for r in rows)
    for key, members in sorted(groups.items()):
        name=clean(members[0].get('Family Candidate') or members[0]['EPOS Name'])
        sku='AKP-'+hashlib.sha256(key.encode()).hexdigest()[:10].upper()
        tracked=[r for r in members if clean(r.get('Stock Tracked')).lower()=='true']
        issues=[]
        if len(tracked)!=1: issues.append('STOCK_OWNER_REQUIRED')
        if any(r['Pipeline Status']=='BLOCK' for r in members): issues.append('SOURCE_REVIEW')
        if any('PRODUCT_ID_CONFLICT' in r.get('Issue Codes','') for r in members): issues.append('PRODUCT_ID_CONFLICT')
        if any(names[clean(r['EPOS Name']).casefold()]!=1 for r in members): issues.append('DUPLICATE_EPOS_NAME')
        owner=tracked[0] if len(tracked)==1 else {}
        denominator=number(owner.get('EPOS Volume Denominator'))
        # Missing denominator is only a single-unit proposal, never staff approval.
        full=denominator if denominator is not None else Decimal(1)
        if full<=0: issues.append('INVALID_VOLUME_DENOMINATOR'); full=None
        stock=number(owner.get('EPOS Total Stock'))
        cost=number(owner.get('EPOS Cost Inc Tax'))
        if owner and stock is None: issues.append('MISSING_STOCK')
        if owner and (cost is None or cost<=0): issues.append('MISSING_COST')
        if len(name)>100: issues.append('QBO_NAME_TOO_LONG')
        canonical_qty=max(stock,Decimal(0))*full if stock is not None and full else None
        unit_cost=cost/full if cost is not None and cost>=0 and full else None
        if stock is not None and stock<0: issues.append('NEGATIVE_ZERO_FLOOR')
        fatal=set(issues)-{'MISSING_COST','NEGATIVE_ZERO_FLOOR'}
        family={'Family key':key,'Proposed QBO name':name,'Proposed AKP SKU':sku,
                'EPOS rows':len(members),'Tracked rows':len(tracked),
                'Stock owner row':owner.get('Row ID',''), 'Proposed canonical unit':'Each / sellable unit (confirm)',
                'Full stock to canonical (proposal)':str(full) if full else '',
                '16 Sep canonical qty (proposal)':str(canonical_qty) if canonical_qty is not None else '',
                'Unit cost inclusive (proposal)':str(unit_cost) if unit_cost is not None else '',
                '16 Sep value inclusive (proposal)':str(canonical_qty*unit_cost) if canonical_qty is not None and unit_cost is not None else '',
                'Status':'BLOCK' if fatal else 'CANDIDATE_NOT_APPROVED',
                'Issues':'; '.join(issues),'Approved canonical unit':'','Approved stock owner row':'',
                'Approved full multiplier':'','Approved loose multiplier':'','Approved purchase multiplier':'',
                'Cost tax basis':'','Verified unit cost':'','Cost evidence':'','Approved by':''}
        families.append(family)
        for r in members:
            mappings.append({'Row ID':r['Row ID'],'Family key':key,'Canonical Family Key':key,'Canonical Unit':'','Staff Approved Purchase Multiplier':'','EPOS Product ID':r.get('EPOS Product ID',''),
                'EPOS Existing SKU':r.get('EPOS Existing SKU',''),'EPOS Name':r['EPOS Name'],
                'Sell on Till':r.get('Sell on Till',''),'Pipeline Status':'BLOCK' if fatal else 'PROVISIONAL',
                'Review Status':'Provisional','Target QBO Item Type':'Inventory','Target QBO Name':name,
                'Target QBO SKU':sku,'Target QBO Item Id':'','Prior sale suggestion (unapproved)':r.get('Proposed Full Unit Multiplier',''),
                'Staff Approved Sale Multiplier':'','Effective Date':'2026-10-01','Approved By':'',
                'Staff question':'Confirm physical unit sold by this button; units consumed from which master Product ID?',
                'Source Product File':r.get('Source Product File',''),'Identity evidence issues':r.get('Issue Codes',''),
                'Sales Product ID Candidates':r.get('Sales Product ID Candidates','')})
        for item in live_names.get(name.casefold(),[]):
            collisions.append({'Family key':key,'Target name':name,'QBO Id':item['Id'],'Type':item['Type'],
                'Active':item['Active'],'Proposed legacy name':'LEGACY — '+item['Name'],
                'Action':'REVIEW_RENAME_ONLY; chat approval required',
                'Name length check':'PASS' if len('LEGACY — '+item['Name'])<=100 else 'BLOCK'})
        # Technical rehearsal only. Cost cannot be posted until tax basis verified.
        if not fatal:
            payloads.append({'dry_run':True,'production_eligible':False,
                'Name':name,'Sku':sku,'Type':'Inventory','TrackQtyOnHand':True,
                'QtyOnHand':0,'InvStartDate':'2026-10-01','AssetAccountRef':{'value':'77'},
                '_proposal':{'unit_cost_inclusive':str(unit_cost) if unit_cost is not None else '',
                             'requires':'approved units, cost tax basis, income/COGS accounts, tax code and 30 Sep stock'}})
    assert len({f['Proposed AKP SKU'] for f in families})==len(families)
    assert sum(f['EPOS rows'] for f in families)==len(rows)
    return families,mappings,payloads,collisions


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sales',type=Path);p.add_argument('--conversion',required=True,type=Path);p.add_argument('--qbo-items',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    rows=read(a.conversion);live=read(a.qbo_items)
    enrichment=enrich_product_ids(rows,read(a.sales)) if a.sales else {}
    families,mappings,payloads,collisions=build(rows,live)
    write(a.out/'canonical_families.csv',families);write(a.out/'product_conversion_review.csv',mappings)
    write(a.out/'legacy_rename_proposals.csv',collisions,['Family key','Target name','QBO Id','Type','Active','Proposed legacy name','Action','Name length check'])
    questions=[]
    for family in families:
        if not family['Issues']:
            continue
        members=[r for r in mappings if r['Family key']==family['Family key']]
        till=any(r['Sell on Till'].lower()=='true' for r in members)
        prompts=[]
        issues=family['Issues']
        if 'STOCK_OWNER_REQUIRED' in issues:
            prompts.append('Give the single master Product ID that owns stock; do child rows repeat that same stock?')
        if 'PRODUCT_ID_CONFLICT' in issues:
            prompts.append('Confirm the current Product ID for each till button using its EPOS setup screen; sales IDs disagree.')
        if 'SOURCE_REVIEW' in issues:
            prompts.append('Confirm what one sale physically contains, canonical unit, full-pack and loose-unit multipliers, and child Master Product Amount.')
        if 'DUPLICATE_EPOS_NAME' in issues:
            prompts.append('Distinguish same-name buttons by Product ID/SKU or mark duplicate; do not use barcode.')
        if 'MISSING_COST' in issues:
            prompts.append('Provide supplier invoice unit cost and VAT basis, or explicitly accept cost 0 pending evidence.')
        if 'NEGATIVE_ZERO_FLOOR' in issues:
            prompts.append('Confirm physical stock is zero or supply counted full/loose units; opening negative stock floors to 0.')
        questions.append({'Priority':1 if till and family['Status']=='BLOCK' else 2 if till else 3,
            'Family':family['Proposed QBO name'],'AKP SKU':family['Proposed AKP SKU'],'On till':till,
            'Issues':issues,'Questions':' '.join(prompts),'Answer':'','Staff name':'','Evidence reference':''})
    questions.sort(key=lambda r:(r['Priority'],r['Family']))
    write(a.out/'staff_questions.csv',questions)
    (a.out/'catalogue_rehearsal.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in payloads))
    summary={'rows_with_product_id':sum(bool(r.get('EPOS Product ID')) for r in rows),'source_rows':len(rows),'candidate_families':len(families),'rehearsal_payloads':len(payloads),
        'blocked_families':sum(f['Status']=='BLOCK' for f in families),'approved_rows':0,'staff_question_families':len(questions),'priority_1_till_blocked':sum(r['Priority']==1 for r in questions),
        'family_issues':dict(Counter(i for f in families for i in f['Issues'].split('; ') if i)),
        'name_collisions':len(collisions),'active_collisions':sum(c['Active'].lower()=='true' for c in collisions),
        'candidate_value_inclusive_partial':str(sum((Decimal(f['16 Sep value inclusive (proposal)']) for f in families if f['Status']!='BLOCK' and f['16 Sep value inclusive (proposal)']),Decimal(0))),
        'inputs':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [a.conversion,a.qbo_items,a.sales] if p is not None},
        **enrichment,
        'status':'REVIEW REQUIRED; no approved production payloads. Candidate family links need EPOS master/child evidence.'}
    (a.out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
