"""READ-ONLY re-check of the open review items (portal "Refresh" on Needs your attention).

Re-plans bills (EPOS POs vs QuickBooks) and banking (till sheet vs Undeposited Funds) into ``--out``:
``<out>/bills/`` and ``<out>/deposits/<day>/``. Both tools run in plan mode, so nothing is posted;
the portal reads the newest plan for each item, so a fixed cause clears and a new one shows.

    python -m code_scripts.akponora_ops.recheck --out <folder>
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def commands(out: Path) -> list[tuple[str, list[str]]]:
    from code_scripts.akponora_ops import bills_sync
    from code_scripts.akponora_ops.daily_run import last_closed_business_date

    day = last_closed_business_date().isoformat()
    py = [sys.executable, "-m"]
    return [
        ("bills", py + ["code_scripts.akponora_ops.bills_sync", "plan", "--from", min(bills_sync.default_from(), day),
                        "--to", day, "--out", str(out / "bills"), "--no-slack"]),
        ("banking", py + ["code_scripts.akponora_ops.uf_deposits", "plan", "--to", day,
                          "--out", str(out / "deposits"), "--no-slack"]),
    ]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = []
    for name, cmd in commands(out):
        print(f"== {name}: {' '.join(cmd[2:])}", flush=True)
        if subprocess.call(cmd) != 0:
            failed.append(name)
    if failed:
        print("re-check did not finish: " + ", ".join(failed))
        return 2
    print("re-check finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
