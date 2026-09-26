"""Pure proposal builders: canonical Inventory opening, non-stock NonInventory
items and item-based purchases.

No HTTP, no credentials, no production posting. Inputs must be staff-reviewed
unit decisions and verified source evidence. Flags stay visible in the output.
"""
from datetime import date
from decimal import Decimal
from code_scripts.operations_controls import purchase_units
from code_scripts.conversion_contract import finite,validate_live_item
from code_scripts.product_conversion import OCTOBER_NONSTOCK_SKU_PREFIX,canonical_product_id


def validate_family(family):
    required=('family_key','name','sku','canonical_unit','stock_owner_id','approved_by','approval_ref',
              'unit_evidence','cost_evidence','cost_tax_basis','income_account_id','cogs_account_id','tax_code_id')
    if any(not str(family.get(k,'')).strip() for k in required):
        raise ValueError('Canonical family is missing approved identity/unit/cost/account evidence')
    if not family['sku'].startswith('AKP-') or len(family['name'])>100:
        raise ValueError('Invalid canonical SKU/name')
    if family['cost_tax_basis'] not in {'exclusive','inclusive','exempt'}:
        raise ValueError('Cost tax basis must be verified')
    for k in ('full_multiplier','loose_multiplier','purchase_multiplier'):
        if finite(family[k])<=0:raise ValueError('Approved multiplier must be positive')
    if finite(family['canonical_unit_cost'])<0:raise ValueError('Unit cost cannot be negative')


def catalogue_drafts(families,counts,*,cutoff_date):
    if cutoff_date!='2026-09-30':raise ValueError('Opening requires the final 30 September count')
    indexed={}
    for count in counts:
        owner=count['stock_owner_id']
        if owner in indexed:raise ValueError('Duplicate stock owner count would double-count inventory')
        if not count.get('source_ref') or count.get('cutoff_date')!=cutoff_date:
            raise ValueError('Every count needs cutoff and source evidence')
        indexed[owner]=count
    seen={k:set() for k in ('family_key','name','sku','stock_owner_id')}
    drafts=[];total=Decimal(0)
    for family in families:
        validate_family(family)
        for key in seen:
            value=str(family[key]).strip().casefold()
            if value in seen[key]:raise ValueError('Duplicate canonical '+key)
            seen[key].add(value)
        count=indexed.get(family['stock_owner_id'])
        if count is None:raise ValueError('Missing count for canonical stock owner')
        raw_qty=finite(count['full_count'])*finite(family['full_multiplier'])+finite(count['loose_count'])*finite(family['loose_multiplier'])
        qty=max(Decimal(0),raw_qty);cost=finite(family['canonical_unit_cost']);flags=[]
        if raw_qty<0:flags.append('NEGATIVE_ZERO_FLOOR')
        if cost==0 and qty>0:flags.append('MISSING_COST_POSITIVE_STOCK')
        payload={'Name':family['name'],'Sku':family['sku'],'Type':'Inventory','TrackQtyOnHand':True,
                 'QtyOnHand':float(qty),'PurchaseCost':float(cost),'InvStartDate':'2026-10-01',
                 'AssetAccountRef':{'value':'77'},'IncomeAccountRef':{'value':str(family['income_account_id'])},
                 'ExpenseAccountRef':{'value':str(family['cogs_account_id'])},
                 'PurchaseTaxCodeRef':{'value':str(family['tax_code_id'])},
                 'SalesTaxCodeRef':{'value':str(family['tax_code_id'])},
                 'PurchaseTaxIncluded':family['cost_tax_basis']=='inclusive'}
        # Actual QBO create impact must still be read from the GL; this is a proposal.
        value=qty*cost;total+=value
        drafts.append({'payload':payload,'family_key':family['family_key'],'flags':flags,'source_ref':count['source_ref'],
                       'quantity':str(qty),'unit_cost':str(cost),'proposed_value':str(value),'production_approved':False})
    if set(indexed)-{f['stock_owner_id'] for f in families}:
        raise ValueError('Count contains unmapped stock owners; resolve before complete valuation')
    return {'drafts':drafts,'proposed_value':str(total),'production_approved':False}


# Income / purchases (200xxx) accounts by EPOS category band, as used for the
# October Inventory catalogue. Unknown categories fail closed.
NONSTOCK_ACCOUNTS={
    'grocery':{'income_id':'1150040024','expense_id':'74'},              # 400100 / 200100
    'alcoholic':{'income_id':'1150040032','expense_id':'1150040033'},    # 400201 / 200201
    'non_alcoholic':{'income_id':'1150040031','expense_id':'1150040034'},# 400202 / 200202
    'non_food':{'income_id':'1150040025','expense_id':'1150040020'},     # 400300 / 200300
}
NONSTOCK_CATEGORY_BAND={
    'ALCOHOLS & SPIRITS':'alcoholic','DRINKS & BEVERAGES':'non_alcoholic',
    'COSMETICS AND TOILETRIES':'non_food','HOUSEHOLD GOODS & PACKAGING MATERIALS':'non_food',
    'STATIONARY AND BOOKSHOP SUPPLIES':'non_food','PROVISIONS AND CEREALS':'grocery',
    'CANNED GOOD, COOK OIL, SWALLOW & BAKING':'grocery','COOKING SPICES & SEASONINGS':'grocery',
    'FROZEN FOODS':'grocery',
}


def nonstock_sku(epos_product_id):
    pid=canonical_product_id(epos_product_id)
    if not pid.isdigit():raise ValueError('Non-stock item needs a numeric EPOS Product ID')
    return OCTOBER_NONSTOCK_SKU_PREFIX+pid


def noninventory_drafts(products):
    """Create proposals for EPOS products with no stock master (one item per product).

    Payload: Name, Sku AKP-NS-{EPOS ProductID}, Type NonInventory, income and
    purchases (200xxx) accounts by category. No asset, quantity or start date.
    """
    drafts=[];seen={'name':set(),'sku':set()}
    for product in products:
        if any(not str(product.get(k,'')).strip() for k in ('epos_product_id','name','category','approved_by','approval_ref')):
            raise ValueError('Non-stock product is missing EPOS id/name/category/approval evidence')
        name=' '.join(str(product['name']).split())
        if len(name)>100:raise ValueError('QBO name too long: '+name)
        if str(product.get('stock_tracked','')).strip().lower() in {'true','yes','1'}:
            raise ValueError('Stock-tracked EPOS product must map to Inventory, not NonInventory: '+name)
        band=NONSTOCK_CATEGORY_BAND.get(' '.join(str(product['category']).split()).upper())
        if band is None:raise ValueError('Unknown EPOS category for account mapping: '+str(product['category']))
        sku=nonstock_sku(product['epos_product_id'])
        for key,value in (('name',name.casefold()),('sku',sku)):
            if value in seen[key]:raise ValueError('Duplicate non-stock '+key+': '+value)
            seen[key].add(value)
        accounts=NONSTOCK_ACCOUNTS[band]
        payload={'Name':name,'Sku':sku,'Type':'NonInventory',
                 'IncomeAccountRef':{'value':accounts['income_id']},
                 'ExpenseAccountRef':{'value':accounts['expense_id']}}
        if str(product.get('tax_code_id','')).strip():
            payload.update(SalesTaxCodeRef={'value':str(product['tax_code_id'])},
                           PurchaseTaxCodeRef={'value':str(product['tax_code_id'])},
                           SalesTaxIncluded=True,PurchaseTaxIncluded=True,Taxable=True)
        drafts.append({'payload':payload,'epos_product_id':canonical_product_id(product['epos_product_id']),
                       'category_band':band,'approval_ref':product['approval_ref'],'production_approved':False})
    return {'drafts':drafts,'production_approved':False}


def bill_draft(family,live_item,*,vendor_id,invoice_no,invoice_date,packs,cost_per_pack,invoice_ref,goods_received_ref):
    validate_family(family)
    if date.fromisoformat(invoice_date)<date(2026,10,1):raise ValueError('New Inventory purchase cannot predate cutover')
    if not all((vendor_id,invoice_no,invoice_ref,goods_received_ref)):
        raise ValueError('Vendor, invoice and received-goods evidence required')
    expected={'Id':family['qbo_item_id'],'Name':family['name'],'Sku':family['sku'],'Type':'Inventory','InvStartDate':'2026-10-01'}
    validate_live_item(live_item,expected)
    units=purchase_units(packs,family['purchase_multiplier'],cost_per_pack)
    return {'payload':{'VendorRef':{'value':str(vendor_id)},'DocNumber':invoice_no,'TxnDate':invoice_date,
        'GlobalTaxCalculation':'TaxInclusive' if family['cost_tax_basis']=='inclusive' else 'TaxExcluded',
        'Line':[{'DetailType':'ItemBasedExpenseLineDetail','Amount':float(units['amount']),
                 'ItemBasedExpenseLineDetail':{'ItemRef':{'value':family['qbo_item_id']},'Qty':float(units['quantity']),
                   'UnitPrice':float(units['unit_cost']),'TaxCodeRef':{'value':family['tax_code_id']}}}]},
        'evidence':[invoice_ref,goods_received_ref],'production_approved':False,
        'requires':'Live vendor/invoice duplicate search, VAT/total reconciliation and explicit chat approval'}
