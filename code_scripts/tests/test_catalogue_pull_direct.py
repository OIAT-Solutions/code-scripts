import tempfile
import unittest
from pathlib import Path

from code_scripts.scripts.akponora_cutover import epos_catalogue_pull as cp


def body(n, total, per=250):
    start = (n - 1) * per
    return {"Data": [{"Id": i} for i in range(start, min(start + per, total))],
            "Metadata": {"Page": n, "Limit": per, "TotalRecords": total}}


class Resp:
    def __init__(self, ok, data):
        self.ok, self.status, self._d = ok, 200 if ok else 500, data

    def json(self):
        return self._d


class FakePage:
    def __init__(self, total, fail_on=None, short=False):
        self.total, self.fail_on, self.short, self.urls = total, fail_on, short, []
        self.request = self

    def wait_for_timeout(self, ms):
        pass

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        n = int(url.split("page=")[1].split("&")[0])
        if n == self.fail_on:
            return Resp(False, {})
        b = body(n, self.total)
        if self.short:
            b["Data"] = b["Data"][:10]
        return Resp(True, b)


class DirectPagesTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.first = {"url": "https://products.eposnowhq.com/api/v4/catalogue/products?page=1&limit=250&archived=false",
                      "headers": {"authorization": "x", ":path": "drop me"}}

    def captured(self, total):
        return [{"url": "page=1&limit=50&archived=false", "body": body(1, total, 50)},
                {"url": "page=1&limit=250&archived=false", "body": body(1, total)}]

    def test_fetches_every_remaining_page_once_and_all_products_arrive(self):
        page, cap = FakePage(6246), self.captured(6246)
        self.assertTrue(cp.fetch_remaining(page, self.first, cap, self.out))
        self.assertEqual(len(page.urls), 24)  # pages 2..25
        self.assertTrue(all("limit=250" in u and "archived=false" in u for u in page.urls))
        self.assertEqual(len(cp.products_from_captures(cap)), 6246)

    def test_a_failed_page_or_missing_products_falls_back_to_ui_paging(self):
        self.assertFalse(cp.fetch_remaining(FakePage(6246, fail_on=7), self.first, self.captured(6246), self.out))
        cap = self.captured(6246)
        self.assertFalse(cp.fetch_remaining(FakePage(6246, short=True), self.first, cap, self.out))
        self.assertEqual(len(cap), 2)  # partial direct pages are discarded before the UI paging

    def test_without_the_first_request_nothing_is_tried(self):
        self.assertFalse(cp.fetch_remaining(FakePage(10), {}, self.captured(10), self.out))


if __name__ == "__main__":
    unittest.main()
