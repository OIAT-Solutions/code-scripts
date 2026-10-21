"""Context processors for epos_qbo app."""

from .dashboard_timezone import get_dashboard_timezone_display


def dashboard_timezone(request):
    """Add dashboard timezone display label so templates can show which TZ drives dates."""
    return {"dashboard_timezone_display": get_dashboard_timezone_display()}


# 13 Oct 2026: the new layout is the only layout. Every signed-in page gets the Nora Mart sidebar/topbar;
# a view that sets its own company_* keys (company workspace, Goldplates) overrides these defaults.
SHELL_TABS = (("/epos-qbo/attention", "attention"), ("/epos-qbo/runs", "runs"), ("/epos-qbo/company-a/", "runs"),
              ("/epos-qbo/logs", "runs"), ("/epos-qbo/schedules", "schedules"))
SHELL_LABELS = {"/epos-qbo/settings": "Portal settings", "/epos-qbo/api-tokens": "QuickBooks connections",
                "/epos-qbo/tools": "Support tools", "/epos-qbo/companies/new": "Add a company",
                "/account": "Account"}


def nora_shell(request):
    user = getattr(request, "user", None)
    path = getattr(request, "path", "") or ""
    if not (user and user.is_authenticated) or "/api/" in path or path.startswith(("/admin/", "/login", "/webhooks")):
        return {}
    active = next((tab for prefix, tab in SHELL_TABS if path.startswith(prefix)), "admin")
    try:
        from .services.company_workspace import shell_context
        ctx = shell_context(request, active)
    except Exception:  # the shell never breaks the page it wraps
        return {}
    if active == "admin":
        ctx["company_tab_label"] = next((label for prefix, label in SHELL_LABELS.items() if path.startswith(prefix)), "Admin")
    return ctx
