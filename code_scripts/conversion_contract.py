"""Read-only validation between conversion, upload and the live QBO catalogue.

Proof is an audit trail, not a signature. Reprocessing raw EPOS evidence remains
part of operator acceptance. No function in this module can post to QBO.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import json

from code_scripts.product_conversion import ProductConversionRegistry, clean, normalize

CUTOVER = date(2026, 10, 1)
HISTORY_ID = '15030'
HISTORY_NAME = 'AKP-UNMAPPED-EPOS-SALES'


def finite(value):
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid numeric conversion evidence') from exc
    if not n.is_finite():
        raise ValueError('Non-finite conversion evidence')
    return n


def validate_upload_frame(frame, config, registry=None):
    """Validate every line before any posting; return exact targets by name."""
    registry = registry or ProductConversionRegistry.from_csv(
        config.product_conversion_file,
        allow_name_fallback=config.product_conversion_allow_name_fallback,
    )
    base_required = {'*SalesReceiptDate', 'Item(Product/Service)', 'ItemQuantity'}
    if not base_required.issubset(frame.columns):
        raise ValueError('Missing conversion evidence; regenerate the sales CSV from raw EPOS data')
    if '_Conversion Proof' not in frame.columns:
        return _validate_legacy_history_frame(frame)
    required = base_required | {'_Conversion Proof'}
    if not required.issubset(frame.columns):
        raise ValueError('Missing conversion evidence; regenerate the sales CSV from raw EPOS data')
    targets = {}
    for _, line in frame.iterrows():
        txn_date = date.fromisoformat(clean(line['*SalesReceiptDate']))
        name = clean(line['Item(Product/Service)'])
        parts = json.loads(line['_Conversion Proof'])
        if not isinstance(parts, list) or not parts:
            raise ValueError('Empty conversion proof')
        for field in ('*ItemAmount', 'TOTAL Sales', 'NET Sales', 'ItemTaxAmount'):
            if field not in frame.columns:
                raise ValueError('Missing monetary control column: ' + field)
            finite(line[field])
        if abs(finite(line['*ItemAmount']) - finite(line['TOTAL Sales'])) > Decimal('0.01'):
            raise ValueError('Gross sales control mismatch')
        if abs(finite(line['TOTAL Sales']) - finite(line['NET Sales']) - finite(line['ItemTaxAmount'])) > Decimal('0.02'):
            raise ValueError('Gross/net/VAT control mismatch')
        expected_quantity = Decimal(0)
        for part in parts:
            if part['mapping_sha256'] != registry.source_sha256 or part['date'] != txn_date.isoformat():
                raise ValueError('Mapping version or business date changed; regenerate from raw EPOS')
            quantity = finite(part['quantity'])
            if part['row_id'] == 'HISTORY_CATCH_ALL':
                if txn_date >= CUTOVER or name != HISTORY_NAME:
                    raise ValueError('Catch-all is restricted to pre-October history')
                target = {'Id': HISTORY_ID, 'Name': HISTORY_NAME, 'Type': 'NonInventory'}
                expected_quantity += quantity
            else:
                rule = registry.resolve(product_name=part['product'], product_id=part['product_id'],
                                        sku=part['sku'], transaction_date=txn_date)
                if rule.row_id != part['row_id'] or name != rule.target_qbo_name:
                    raise ValueError('Exact mapped target mismatch')
                if not rule.target_qbo_item_id:
                    raise ValueError('Mapped QBO Item Id is required at upload')
                if txn_date >= CUTOVER and (rule.target_qbo_type != 'Inventory' or not rule.target_qbo_sku.startswith('AKP-')):
                    raise ValueError('October requires Inventory with an AKP- SKU')
                target = {'Id': rule.target_qbo_item_id, 'Name': rule.target_qbo_name,
                          'Sku': rule.target_qbo_sku, 'Type': rule.target_qbo_type}
                if rule.target_qbo_type == 'Inventory':
                    if txn_date < CUTOVER:
                        raise ValueError('New Inventory cannot receive historical sales')
                    target.update(InvStartDate='2026-10-01', AssetAccountRef={'value': '77'})
                expected_quantity += quantity * rule.sale_multiplier
            if name in targets and targets[name] != target:
                raise ValueError('Conflicting target identities in sales file')
            targets[name] = target
        if abs(expected_quantity - finite(line['ItemQuantity'])) > Decimal('0.00000001'):
            raise ValueError('Converted quantity differs from approved multiplier')
    return targets


def _validate_legacy_history_frame(frame):
    """Accept a CSV transformed before the proof column existed, history only.

    Allowed only when every line is dated before the cutover AND every line already
    targets the catch-all item. Anything else must be regenerated from raw EPOS.
    """
    for _, line in frame.iterrows():
        txn_date = date.fromisoformat(clean(line['*SalesReceiptDate']))
        if txn_date >= CUTOVER:
            raise ValueError('Missing conversion evidence for an October-or-later date; regenerate from raw EPOS')
        if clean(line['Item(Product/Service)']) != HISTORY_NAME:
            raise ValueError('Legacy CSV without conversion proof may only contain catch-all history lines; '
                             'regenerate from raw EPOS')
        for field in ('*ItemAmount', 'TOTAL Sales', 'NET Sales', 'ItemTaxAmount'):
            if field in frame.columns:
                finite(line[field])
    return {HISTORY_NAME: {'Id': HISTORY_ID, 'Name': HISTORY_NAME, 'Type': 'NonInventory'}}


def validate_live_item(item, expected):
    for field in ('Id', 'Name', 'Type', 'Sku', 'InvStartDate'):
        if field in expected and clean(item.get(field)) != expected[field]:
            raise ValueError(f'QBO {field} mismatch for {expected["Name"]}: expected {expected[field]}')
    if item.get('Active') is not True:
        raise ValueError('Mapped QBO item is inactive or Active is missing')
    if expected['Type'] == 'Inventory':
        if item.get('TrackQtyOnHand') is not True:
            raise ValueError('Mapped Inventory must track quantity')
        if str((item.get('AssetAccountRef') or {}).get('value', '')) != '77':
            raise ValueError('Mapped Inventory must use Inventory Asset Id 77')
