import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pandas as pd

from code_scripts.company_config import CompanyConfig
from code_scripts.conversion_contract import validate_upload_frame, validate_live_item
from code_scripts.product_conversion import ProductConversionRegistry, MappingValidationError, ProductResolutionError
from code_scripts.tests.test_product_conversion import approved_row, write_mapping, sales_frame, Config
from code_scripts.transform import transform_dataframe_unified
from code_scripts.scripts.install_conversion_mapping import install
from code_scripts import qbo_upload


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.row=approved_row(**{'Target QBO Item Type':'Inventory','Target QBO Item Id':'90001',
            'Target QBO Name':'Wine bottle','Target QBO SKU':'AKP-WINE','Effective Date':'2026-10-01',
            'Staff Approved Sale Multiplier':'24','EPOS Name':'Wine crate'})
        self.path=write_mapping(self.root,[self.row]);self.config=Config(self.path)
        from datetime import date
        self.config.product_conversion_fail_closed_from=date(2026,10,1)
        self.frame=sales_frame(Product='Wine crate',**{'Date/Time':'2026-10-01 10:00:00'})

    def converted(self):
        return transform_dataframe_unified(self.frame,self.config)

    def test_crate_24_targets_one_bottle_item(self):
        out=self.converted();targets=validate_upload_frame(out,self.config)
        self.assertEqual(out.iloc[0]['ItemQuantity'],24)
        self.assertEqual(targets['Wine bottle']['Id'],'90001')
        self.assertEqual(out.iloc[0]['*ItemAmount'],120000)

    def test_aggregation_preserves_all_source_proofs(self):
        self.frame=pd.concat([self.frame,self.frame],ignore_index=True)
        out=self.converted();validate_upload_frame(out,self.config)
        self.assertEqual(len(out),1);self.assertEqual(out.iloc[0]['ItemQuantity'],48)
        self.assertEqual(len(json.loads(out.iloc[0]['_Conversion Proof'])),2)

    def test_changed_mapping_blocks_stale_transform(self):
        out=self.converted();self.row['Staff Approved Sale Multiplier']='12';write_mapping(self.root,[self.row])
        with self.assertRaisesRegex(ValueError,'version'):validate_upload_frame(out,self.config)

    def test_tampered_quantity_blocks(self):
        out=self.converted();out.loc[0,'ItemQuantity']=576
        with self.assertRaisesRegex(ValueError,'quantity'):validate_upload_frame(out,self.config)

    def test_changed_date_blocks(self):
        out=self.converted();out.loc[0,'*SalesReceiptDate']='2026-10-02'
        with self.assertRaisesRegex(ValueError,'date'):validate_upload_frame(out,self.config)

    def test_missing_proof_blocks_old_csv(self):
        with self.assertRaisesRegex(ValueError,'evidence'):
            validate_upload_frame(self.converted().drop(columns=['_Conversion Proof']),self.config)

    def test_noninventory_october_blocked(self):
        self.row['Target QBO Item Type']='Service';write_mapping(self.root,[self.row])
        with self.assertRaises(ProductResolutionError):self.converted()

    def test_blank_and_nonfinite_quantity_blocked(self):
        self.frame['Quantity']=self.frame['Quantity'].astype(object)
        for value in ('', 'NaN', 'Infinity', '-Infinity'):
            with self.subTest(value=value):
                self.frame.loc[0,'Quantity']=value
                with self.assertRaises(ProductResolutionError):self.converted()

    def test_nonfinite_multiplier_blocked(self):
        for value in ('NaN','Infinity','0','-1'):
            with self.subTest(value=value):
                self.row['Staff Approved Sale Multiplier']=value;write_mapping(self.root,[self.row])
                with self.assertRaises(MappingValidationError):self.converted()

    def test_invalid_date_cannot_be_skipped(self):
        self.frame.loc[0,'Date/Time']='not-a-date'
        with self.assertRaises(ProductResolutionError):self.converted()

    def test_business_date_controls_cutover(self):
        # Sale after midnight still belongs to Sep 30 trading day, so history fallback.
        self.config.trading_day_enabled=True
        self.frame.loc[0,'Date/Time']='2026-10-01 02:00:00'
        out=transform_dataframe_unified(self.frame,self.config,target_date='2026-09-30')
        targets=validate_upload_frame(out,self.config)
        self.assertEqual(next(iter(targets.values()))['Id'],'15030')

    def test_october_unknown_cannot_catchall(self):
        self.frame.loc[0,'Product']='unknown'
        with self.assertRaises(ProductResolutionError):self.converted()

    def test_live_exact_identity(self):
        expected=validate_upload_frame(self.converted(),self.config)['Wine bottle']
        live={**expected,'Active':True,'TrackQtyOnHand':True}
        validate_live_item(live,expected)
        for field,value in [('Id','90002'),('Sku',''),('Type','Service'),('Name','Wrong'),
                            ('InvStartDate','2026-09-01'),('Active',False),('TrackQtyOnHand',False),
                            ('AssetAccountRef',{'value':'120000'})]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):validate_live_item({**live,field:value},expected)

    def test_upload_resolves_id_and_never_calls_create(self):
        targets=validate_upload_frame(self.converted(),self.config)
        response=mock.Mock(status_code=200)
        response.json.return_value={'Item':{**targets['Wine bottle'],'Active':True,'TrackQtyOnHand':True}}
        with mock.patch.object(qbo_upload,'_make_qbo_request',return_value=response) as request, mock.patch.object(qbo_upload,'get_or_create_item_id') as create:
            result={};qbo_upload.resolve_conversion_items(['Wine bottle'],self.config,mock.Mock(),'9341455406194328',result,exact_targets=targets)
        self.assertIn('/item/90001?',request.call_args.args[1]);create.assert_not_called()
        self.assertEqual(result['Wine bottle']['item_id'],'90001')

    def test_conflicting_family_ids_blocked(self):
        other={**self.row,'Row ID':'2','EPOS Name':'Wine bottle','EPOS Product ID':'42','EPOS Existing SKU':'EACH','Target QBO Item Id':'90002'}
        write_mapping(self.root,[self.row,other])
        with self.assertRaises(MappingValidationError):ProductConversionRegistry.from_csv(self.path)

    def test_state_path_and_immutable_cutover(self):
        path=Path(__file__).resolve().parents[1]/'companies/company_a.json'
        with mock.patch.dict('os.environ',{'STATE_ROOT':str(self.root),'COMPANY_A_PRODUCT_CONVERSION_FAIL_CLOSED_FROM':'2027-01-01'}):
            config=CompanyConfig(path)
            self.assertEqual(config.product_conversion_file,self.root/'mappings/company_a/approved.csv')
            self.assertEqual(str(config.product_conversion_fail_closed_from),'2026-10-01')

    def test_atomic_install_retains_previous_version(self):
        destination=self.root/'state/approved.csv';destination.parent.mkdir();destination.write_text('old')
        raw=self.path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        receipt=install(self.path,destination,digest,'chat approval reference')
        self.assertEqual(destination.read_bytes(),raw);self.assertEqual(receipt['rows'],1)
        self.assertEqual((destination.parent/'versions'/f'{hashlib.sha256(b"old").hexdigest()}.csv').read_text(),'old')
        with self.assertRaises(ValueError):install(self.path,destination,'wrong','approval')
        self.assertEqual(destination.read_bytes(),raw)

    def test_same_family_cannot_create_separate_pack_stock(self):
        other={**self.row,'Row ID':'2','EPOS Name':'Wine bottle','EPOS Product ID':'42','EPOS Existing SKU':'EACH',
               'Target QBO Item Id':'90002','Target QBO Name':'Wine pack inventory','Target QBO SKU':'AKP-PACK'}
        write_mapping(self.root,[self.row,other])
        with self.assertRaisesRegex(MappingValidationError,'One canonical family'):
            ProductConversionRegistry.from_csv(self.path)

    def test_inventory_requires_approved_canonical_unit(self):
        self.row['Canonical Unit']='';write_mapping(self.root,[self.row])
        with self.assertRaises(MappingValidationError):self.converted()

    def test_missing_approval_prevents_any_post(self):
        with mock.patch.dict('os.environ',{},clear=True), mock.patch.object(qbo_upload,'_make_qbo_request') as request:
            with self.assertRaises(ValueError):
                qbo_upload.send_sales_receipt({'DocNumber':'SR-1','TxnDate':'2026-10-01'},object(),'9341455406194328')
            request.assert_not_called()

    def test_invalid_source_date_not_hidden_by_business_date_override(self):
        self.frame.loc[0,'Date/Time']='bad-date';self.config.trading_day_enabled=True
        with self.assertRaises(ProductResolutionError):
            transform_dataframe_unified(self.frame,self.config,target_date='2026-09-30')

    def test_name_fallback_must_be_unique_even_with_unapproved_duplicate(self):
        other={**self.row,'Row ID':'2','EPOS Product ID':'42','EPOS Existing SKU':'OTHER','Review Status':'Provisional'}
        write_mapping(self.root,[self.row,other])
        with self.assertRaisesRegex(ProductResolutionError,'AMBIGUOUS_NAME'):self.converted()

    def test_epos_float_id_representation_resolves_exact_integer_id(self):
        self.frame['ProductId']=float(self.row['EPOS Product ID'])
        out=self.converted()
        self.assertEqual(validate_upload_frame(out,self.config)['Wine bottle']['Id'],'90001')

    def test_wrong_business_day_cannot_relabel_source_sales(self):
        self.config.trading_day_enabled=True
        with self.assertRaisesRegex(ProductResolutionError,'SOURCE_DATE_OUTSIDE_BUSINESS_DAY'):
            transform_dataframe_unified(self.frame,self.config,target_date='2026-09-30')
