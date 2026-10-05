# Portal UX: first local checkpoint

Branch: `codex/oiat-portal-ux`, based on `bd45831`.
Worktree: `/Users/marvinmokolo/.codex/worktrees/oiat-portal-ux/code-scripts`.

## Implemented

- Tasks and Home share the closed trading-date rule. Non-Company-A blockers also use confirmed, non-preview artifact evidence.
- An incomplete till sheet does not appear as zero or produce a calculated shortfall. A genuinely entered zero remains a valid comparable amount.
- List-valued deposit activity becomes a count; raw bank dictionaries no longer appear as headline facts.
- Deposits show incomplete sheets separately from ready/review records.
- Home and company header use Open tasks; Home describes Goldplates' sales-only monitoring scope.
- Home/company/deposit title eyebrows removed. This is the first correctness slice, not the completed visual redesign.

## Preview

`http://127.0.0.1:8014/epos-qbo/dashboard/` is a local Django preview with synthetic company names and evidence. Account `preview`, password `local-preview-only`, has no operator permissions. No production tokens are loaded and no scheduler is started. Missing connection warnings are expected.

Synthetic state is `/private/tmp/oiat-portal-ux-preview`, separate from production/runtime. Preview seeded via `/private/tmp/oiat-portal-ux-seed.py`; these temporary assets are local and can disappear on reboot. The tracked application changes and plan remain in the managed worktree.

Start with the existing Python environment:

```sh
STATE_ROOT=/private/tmp/oiat-portal-ux-preview OIAT_COMPANIES_DIR=code_scripts/companies DJANGO_DEBUG=1 '/Users/marvinmokolo/Developer Projects/OIAT/code-scripts/.venv/bin/python' manage.py runserver 127.0.0.1:8014 --noreload
```

Browser verification: Home and Deposits loaded; missing sheet displays Not entered without a difference; activity displays numeric counts; incomplete sheets have a separate count; Home title eyebrow is absent. Desktop Home had equal viewport/document widths. Mobile and alternate-role journeys remain for the larger redesign.

## Validation

Targeted portal suites passed 108 tests before the final summary/template refinements. The final full portal suite passed **542 tests** in 42.1 seconds. `git diff --check` passed. Tests use isolated synthetic state. Earlier full runs exposed sandbox fixture-write restrictions, a duplicated summary keyword corrected locally, and two old Home-label assertions updated to the requested copy.

## Server inspection and next work

Read-only SSH confirmed checkout `bd45831`, running web/scheduler/akponora-ops and existing unrelated untracked files. No server files or containers were changed; image revision is still unverified.

Next: schedules consolidation and business evidence presentation, then capability-driven company Overview. Agency registry/integration work belongs in operations-console. Preview review precedes any deployment request. Financial approval, queue, SHA, lock and audit mechanisms remain intact.
