# Archive: historical Akponora investigation helpers

These scripts come from the June to September 2026 Akponora / NORA (company_a) COGS and inventory investigations. They are kept unchanged, for reference only.

- They contain hard-coded local paths (`/Users/...`, `~/Downloads/...`) and one-off dates.
- They are not maintained or tested, and they are not part of the cutover toolset. Use the tools in the parent folder instead (see `../README.md`).
- None of them writes to QBO or EPOS. They read local exports and write local files.

| File | What it was for |
| --- | --- |
| `akponora_build_product_conversion.py` | Provisional product-conversion data from the July QBO baseline and an EPOS snapshot (superseded by `build_canonical`) |
| `akponora_build_product_conversion_workbook.mjs` | Node workbook builder for that conversion output |
| `akponora_export_may_epos_flags.py` | Flags May 2026 EPOS lines with zero cost or zero net sales |
| `akponora_may_journal_check.py` | May 2026 COGS journal cross-check against EPOS |
| `akponora_prepare_wait_work.py` | Backfill dates, EPOS stock values and journal candidates (August–September) |
