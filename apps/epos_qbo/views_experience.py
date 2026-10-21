from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET


@login_required
@require_GET
def admin_home(request):
    if not (request.user.is_staff or request.user.has_perm("epos_qbo.can_manage_portal_settings")):
        return HttpResponseForbidden("Administrator access is required.")
    from .views import _nav_context
    context = _nav_context()
    context["breadcrumbs"] = [{"label": "Home", "url": reverse("epos_qbo:overview")}, {"label": "Admin", "url": None}]
    return render(request, "epos_qbo/admin_home.html", context)


@login_required
@require_GET
def daily_activity(request):
    """Preserve bookmarked Logs URLs and their company/date filters."""
    query = request.GET.copy()
    if query.get("company_key"):
        query["company"] = query.pop("company_key")[0]
    if query.get("date_from"):
        query["from"] = query.pop("date_from")[0]
    if query.get("date_to"):
        query["to"] = query.pop("date_to")[0]
    url = reverse("epos_qbo:runs")
    return redirect(url + ("?" + query.urlencode() if query else ""))
