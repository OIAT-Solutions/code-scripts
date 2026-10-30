"""26 Oct: Received stock takes are read once; empty ones are accepted from the fast HTML read."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from code_scripts.akponora_ops import epos_adjustments as ea

CELLS_A = ["0", "Plot C, Golf Road", "System", "26/10/2026 16:59:58", ""]
CELLS_B = ["0", "Plot C, Golf Road", "Esther Aribatis", "26/10/2026 16:07:37", ""]


def detail(cells, tid, items=True, status="Received"):
    return {"url": f"https://www.eposnowhq.com/Pages/BackOffice/StockAdjustmentDetails.aspx?TransferID={tid}",
            "info": [["Stock Adjustment Info"], ["Location", cells[1]], ["Ref. Number", "0"], ["Status", status],
                     ["Staff", cells[2]], ["Date", cells[3]], ["Note", ""]],
            "items": [["Product", "Cost", "Quantity"], ["ABSOLUTE LIP GLOSS", "₦170.54", "+36"]] if items else None,
            "tables": ["MainContent_dvTransfers"] + (["MainContent_gvItems"] if items else [])}


class ReusableTests(unittest.TestCase):
    def test_only_a_matching_received_detail_is_reused(self):
        self.assertTrue(ea.reusable(detail(CELLS_A, 1), CELLS_A))
        self.assertFalse(ea.reusable(detail(CELLS_A, 1, status="Pending"), CELLS_A))
        self.assertFalse(ea.reusable(detail(CELLS_A, 1), CELLS_B))
        self.assertFalse(ea.reusable({**detail(CELLS_A, 1), "_list_cells": CELLS_B}, CELLS_A))

    def test_cache_keeps_recent_details_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.jsonl"
            old = detail(["0", "Plot C, Golf Road", "System", "01/09/2026 10:00:00", ""], 2)
            path.write_text("\n".join(json.dumps({"key": k, "detail": d}) for k, d in
                                      (("a", detail(CELLS_A, 1)), ("old", old), ("bad", {"info": None}))) + "\n")
            got = ea.load_cache(path, datetime(2026, 10, 26))
            self.assertEqual(list(got), ["a"])
            self.assertEqual(len(path.read_text().splitlines()), 1)


class ScrapeTests(unittest.TestCase):
    def scrape(self, cache_lines, html_for):
        tmp = Path(tempfile.mkdtemp())
        cache = tmp / "received_details.jsonl"
        cache.write_text("".join(json.dumps(x) + "\n" for x in cache_lines))
        rows = [{"cells": CELLS_A, "btn": "btnA"}, {"cells": CELLS_B, "btn": "btnB"}]
        handlers, clicked = [], []
        page = mock.MagicMock()
        page.locator.return_value.count.return_value = 0

        def evaluate(js, arg=None):
            if js is ea.ROWS_JS:
                return rows
            if js is ea.HTML_DETAIL_JS:
                return html_for[arg.rsplit("=", 1)[1]]
            raise AssertionError(js)

        def click(sel):
            clicked.append(sel)
            tid = {"#btnA": "1", "#btnB": "2"}[sel]
            route = mock.Mock()
            route.fetch.return_value = mock.Mock(
                headers={"location": f"/Pages/BackOffice/StockAdjustmentDetails.aspx?TransferID={tid}"}, status=302)
            handlers[-1](route, mock.Mock(method="POST"))

        page.evaluate.side_effect = evaluate
        page.route.side_effect = lambda pattern, h: handlers.append(h)
        page.click.side_effect = click
        pw = mock.MagicMock()
        pw.chromium.launch.return_value.new_context.return_value.new_page.return_value = page
        ctx = mock.MagicMock(); ctx.__enter__.return_value = pw
        with mock.patch("playwright.sync_api.sync_playwright", return_value=ctx), \
             mock.patch.object(ea, "company_config"), mock.patch.object(ea, "epos_login"), \
             mock.patch.object(ea, "select_calendar_date"):
            ea.scrape_adjustments(tmp, "2026-10-26", "takes", "company_a", through_date="2026-10-27",
                                  cache_path=cache)
        out = [json.loads(x) for x in (tmp / "epos_stocktakes_details.jsonl").read_text().splitlines()]
        metrics = json.loads((tmp / "capture_metrics.json").read_text())
        return out, metrics, clicked, page, cache

    def test_known_take_is_reused_and_empty_take_needs_no_browser_page(self):
        known = {"key": f"stocktakes|{json.dumps(CELLS_A)}", "detail": {**detail(CELLS_A, 1), "_list_cells": CELLS_A}}
        out, metrics, clicked, page, cache = self.scrape([known], {"2": {**detail(CELLS_B, 2, items=False),
                                                                         "_has_pager": False}})
        self.assertEqual(clicked, ["#btnB"])
        self.assertEqual([d["url"][-1] for d in out], ["1", "2"])
        self.assertIsNone(out[1]["items"])
        self.assertEqual((metrics["reused_details"], metrics["empty_html_details"],
                          metrics["browser_detail_fallbacks"]), (1, 1, 0))
        page.context.new_page.assert_not_called()
        self.assertEqual(len(cache.read_text().splitlines()), 2)  # the empty Received take is remembered too

    def test_without_cache_everything_is_read(self):
        html = {"1": {**detail(CELLS_A, 1), "_has_pager": False}, "2": {**detail(CELLS_B, 2), "_has_pager": False}}
        out, metrics, clicked, _, _ = self.scrape([], html)
        self.assertEqual(clicked, ["#btnA", "#btnB"])
        self.assertEqual((metrics["reused_details"], metrics["fresh_html_details"]), (0, 2))


if __name__ == "__main__":
    unittest.main()
