"""'Banked' / 'Not banked' notes next to each day's title in the till sheet. No network."""
from __future__ import annotations

import unittest

from code_scripts.akponora_ops import till_sheet_marks as tm


class FakeValues:
    def __init__(self, tabs):
        self.tabs, self.updates = tabs, []

    def get(self, spreadsheetId, range, valueRenderOption):
        tab = range.split("'")[1]
        return Exec({"values": self.tabs.get(tab, [])})

    def batchUpdate(self, spreadsheetId, body):
        assert body["valueInputOption"] == "RAW"
        self.updates += body["data"]
        return Exec({})


class Exec:
    def __init__(self, out):
        self.out = out

    def execute(self):
        return self.out


class FakeService:
    def __init__(self, tabs):
        self.v = FakeValues(tabs)

    def spreadsheets(self):
        return self

    def values(self):
        return self.v


def block(title, b=""):
    return [[title, b] if b else [title], ["SYSTEM 1 SALES BREAKDOWN"], ["CASH", 1000], [""]]


class SheetMarksTests(unittest.TestCase):
    def tabs(self):
        sep = (block("Friday 25th September 2026 NORA MINI MART")
               + block("Tuesday 29th September 2026 NORA MINI MART", "⏸ Not banked: cash box blank")
               + block("Wednesday 30th September 2026 NORA MINI MART", "checked by Ada"))
        oct_ = block("Thursday 1st October 2026 NORA MINI MART")
        return {"Sep 2026": sep, "Oct 2026": oct_}

    def state(self):
        return {"days": {
            "2026-09-25": {"status": "DEPOSITED", "receipts_total": "4905275.00", "updated_at": "2026-10-04T17:12:00+00:00"},
            "2026-09-29": {"status": "HELD", "reason": "sheet total N4,392,900.00 vs receipts N5,097,324.99: over the tolerance"},
            "2026-09-30": {"status": "DEPOSITED", "receipts_total": "3965950.00", "updated_at": "2026-10-03T20:00:47+00:00"},
            "2026-10-01": {"status": "WAITING_SHEET", "reason": "CASH (System 1) box is blank (type 0 if there was no cash)"},
            "2026-10-02": {"status": "NO_SALES", "reason": "no SalesReceipts in QBO for 2026-10-02 yet"},
        }}

    def test_writes_only_column_b_of_title_rows_and_never_over_typed_text(self):
        svc = FakeService(self.tabs())
        res = tm.sync_marks(self.state(), sheet_id="x", service=svc)
        got = {u["range"]: u["values"][0][0] for u in svc.v.updates}
        self.assertEqual(got, {
            "'Sep 2026'!B1": "✅ Banked · ₦4,905,275 · 4 Oct 18:12",
            "'Sep 2026'!B5": "⏸ Not banked: till sheet and sales don't agree",
        })  # 1 Oct (still being filled in) and 2 Oct (no sales yet) get no note
        self.assertEqual(res["kept_other_content"], ["2026-09-30"])  # someone typed there: left alone
        self.assertEqual(res["not_found"], [])

    def test_unchanged_marks_are_not_rewritten_and_errors_never_raise(self):
        tabs = self.tabs()
        tabs["Sep 2026"][0] = ["Friday 25th September 2026 NORA MINI MART", "✅ Banked · ₦4,905,275 · 4 Oct 18:12"]
        tabs["Oct 2026"][0] = ["Thursday 1st October 2026 NORA MINI MART", "⏸ Not banked: old reason"]
        svc = FakeService(tabs)
        days = self.state()["days"]
        res = tm.sync_marks({"days": {k: days[k] for k in ("2026-09-25", "2026-10-01")}}, sheet_id="x", service=svc)
        self.assertEqual(res["unchanged"], 1)  # 25 Sep already correct
        self.assertEqual(svc.v.updates, [{"range": "'Oct 2026'!B1", "values": [[""]]}])  # stale ⏸ cleared
        broken = tm.sync_safely(self.state(), sheet_id="x", key_path="/nonexistent.json", env={})
        self.assertTrue(broken["error"])
        self.assertTrue(tm.sync_safely(self.state(), sheet_id="x", key_path=None, env={tm.ENABLED_ENV: "0"})["disabled"])


if __name__ == "__main__":
    unittest.main()
