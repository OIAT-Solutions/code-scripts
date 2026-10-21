import unittest

from code_scripts.scripts.akponora_cutover._common import goto_retry


class Page:
    def __init__(self, fails):
        self.fails, self.calls, self.waits = fails, [], 0

    def goto(self, url, timeout=None):
        self.calls.append(timeout)
        if len(self.calls) <= self.fails:
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")

    def wait_for_timeout(self, ms):
        self.waits += 1


class GotoRetryTests(unittest.TestCase):
    def test_slow_epos_page_is_retried_then_loads(self):  # 9 Oct 2026 PO list timeout
        page = Page(fails=2)
        goto_retry(page, "https://epos/po")
        self.assertEqual(page.calls, [90000, 90000, 90000])

    def test_gives_up_after_the_last_attempt(self):
        with self.assertRaises(TimeoutError):
            goto_retry(Page(fails=5), "https://epos/po")


if __name__ == "__main__":
    unittest.main()
