"""Automatic supplier linking on business name and product history (owner rules, 11 Oct 2026)."""
import unittest

from code_scripts.akponora_ops import vendors as v

VENDORS = {i: {"Id": i, "DisplayName": n, "Active": True} for i, n in (
    ("83", "LOLYWAS VENTURES"), ("119", "BABEEMA CONNECT ENTERPRISES"), ("12", "Geesavos Ventures"),
    ("261", "ALPINE FRESH TABLE WATER"), ("63", "MATTA WATER"), ("91", "GODS FAVOUR TRADING (AISHAT AYINKE)"),
    ("70", "GOD MIRACLE TRADING INVOICE (USMAN TAWAKALA)"), ("199", "GOD BLESS TRADING"), ("5", "ADA FOODS"),
    ("6", "ADA FOODS NIGERIA LIMITED"))}
TISSUE = ["ROSE PLUS-TISSUE PAPER_2PLYpk of 48", "SOFTWAVE-TISSUE PAPERpk of 48", "BOUNTY-2PLY PAPER TOWELS*12"]


def index(entries):
    out = {}
    for vid, names in entries:
        for n in names:
            out.setdefault(v.product_key(n), {})[vid] = 1
    return out


def plan(name, products=(), product_index=None, vendors=VENDORS):
    return v.plan_actions([{"key": v.vendor_key(name), "name": name, "products": set(products), "po_refs": {"1"}}],
                          vendors, product_index=product_index or {})[0]


class CoreNameTests(unittest.TestCase):
    def test_brackets_quotes_and_generic_words_are_ignored(self):
        self.assertEqual(v.core_name('"LOLYWAS VENTURES(MUSTAPHA)'), "LOLYWAS")
        self.assertEqual(v.core_name("BABEEMA CONNECT ENT"), v.core_name("BABEEMA CONNECT ENTERPRISES"))
        self.assertEqual(v.core_name("GEESAVOS ENTERPRISES"), v.core_name("Geesavos Ventures"))
        self.assertEqual(v.core_name("GODS FAVOUR TRADING INVOICE(AISHAT)"), v.core_name("GODS FAVOUR TRADING (AISHAT AYINKE)"))
        self.assertEqual(v.core_name("ENT LTD"), "")

    def test_product_key_matches_epos_and_qbo_spellings(self):
        self.assertEqual(v.product_key("COSMETICS AND TOILETRIES:LEGACY — ROSE PLUS-TISSUE PAPER_2PLY*48"),
                         v.product_key("ROSE PLUS-TISSUE PAPER_2PLYpk of 48*1"))
        self.assertEqual(v.product_key("COSMETICS AND TOILETRIES:BOUNTY-2PLY PAPER TOWELS*12 (deleted-1)"),
                         v.product_key("BOUNTY-2PLY PAPER TOWELS*12"))


class AutoLinkTests(unittest.TestCase):
    def test_this_weeks_holds_link_automatically(self):
        for typed, vid in (('"LOLYWAS VENTURES(MUSTAPHA)', "83"), ("BABEEMA CONNECT ENT", "119"),
                           ("GEESAVOS ENTERPRISES", "12")):
            with self.subTest(typed=typed):
                act = plan(typed)
                self.assertEqual((act["state"], act["vendor_id"]), (v.LINK, vid), act["detail"])
                self.assertIn("linked automatically", act["detail"])

    def test_typo_links_only_when_the_products_agree(self):
        water = ["ALPINE FRESH TABLE WATER 75CL*12"]
        act = plan("ALPINE FREAS TABLE WATER", water, index([("261", water)]))
        self.assertEqual((act["state"], act["vendor_id"]), (v.LINK, "261"), act["detail"])
        self.assertEqual(plan("ALPINE FREAS TABLE WATER", water)["state"], v.HOLD_NEAR)  # no product history: ask

    def test_products_alone_never_link_but_name_the_likely_supplier(self):
        vendors = {k: x for k, x in VENDORS.items() if k != "91"} | {"91": {"Id": "91", "DisplayName": "AISHAT AYINKE", "Active": True}}
        act = plan("GODS FAVOUR TRADING INVOICE(AISHAT)", TISSUE, index([("91", TISSUE)]), vendors)
        self.assertEqual(act["state"], v.HOLD_NEAR)
        self.assertEqual(act["vendor_id"], "")
        self.assertIn("3 of 3 products previously billed by 91 AISHAT AYINKE", act["detail"])
        self.assertTrue(act["candidates"][0].startswith("91 AISHAT AYINKE"))

    def test_name_match_with_contradicting_products_is_held(self):
        act = plan("LOLYWAS ENT", TISSUE, index([("91", TISSUE), ("83", ["SOMETHING ELSE"])]))
        self.assertEqual(act["state"], v.HOLD_NEAR)
        self.assertIn("none of these products", act["detail"])

    def test_owner_rule_god_miracle_trading_is_one_supplier(self):
        # 12 Oct 2026: GOD MIRACLE TRADING(IKENNA)'s products had been billed by another vendor; owner: same supplier
        act = plan("GOD MIRACLE TRADING(IKENNA)", TISSUE, index([("201", TISSUE)]))
        self.assertEqual((act["state"], act["vendor_id"]), (v.LINK, "70"), act["detail"])
        self.assertIn("owner rule", act["detail"])

    def test_two_vendors_with_the_same_core_are_not_guessed(self):
        act = plan("ADA FOODS LTD")
        self.assertNotEqual(act["state"], v.LINK)


if __name__ == "__main__":
    unittest.main()
