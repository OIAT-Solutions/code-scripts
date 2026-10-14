(function () {
    'use strict';

    var compactKey = 'nora-ops-sidebar';
    var compact = localStorage.getItem(compactKey) === 'collapsed';
    var body = document.body;
    var mobileSidebar = document.getElementById('nora-mobile-sidebar');
    var scrim = document.getElementById('nora-scrim');
    var menu = document.getElementById('nora-menu');

    function updateCollapseButtons() {
        document.querySelectorAll('.nora-collapse').forEach(function (button) {
            var expanded = !body.classList.contains('nora-compact');
            button.setAttribute('aria-expanded', String(expanded));
            button.setAttribute('aria-label', expanded ? 'Collapse sidebar' : 'Expand sidebar');
            button.title = expanded ? 'Collapse sidebar' : 'Expand sidebar';
            button.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' + (expanded ? 'M15 6l-6 6 6 6' : 'M9 6l6 6-6 6') + '"/></svg>';
        });
    }

    function closeMobile() {
        if (!mobileSidebar || !menu) return;
        mobileSidebar.classList.remove('is-open');
        scrim.hidden = true;
        menu.setAttribute('aria-expanded', 'false');
        menu.setAttribute('aria-label', 'Open navigation');
    }

    function openMobile() {
        if (!mobileSidebar || !menu) return;
        mobileSidebar.classList.add('is-open');
        scrim.hidden = false;
        menu.setAttribute('aria-expanded', 'true');
        menu.setAttribute('aria-label', 'Close navigation');
        var firstLink = mobileSidebar.querySelector('a[href]');
        if (firstLink) firstLink.focus();
    }

    body.classList.toggle('nora-compact', compact);
    updateCollapseButtons();
    document.querySelectorAll('.nora-collapse').forEach(function (button) {
        button.addEventListener('click', function () {
            compact = !body.classList.contains('nora-compact');
            body.classList.toggle('nora-compact', compact);
            localStorage.setItem(compactKey, compact ? 'collapsed' : 'expanded');
            updateCollapseButtons();
        });
    });

    // Remember navigation per signed-in user and company, never copy the outgoing tab.
    document.querySelectorAll('.nora-company-switcher').forEach(function (switcher) {
        var prefix = 'nora-ops-last-tab:' + switcher.dataset.navigationUser + ':';
        var current = switcher.dataset.companyKey;
        var active = switcher.dataset.activeTab;
        var ownLink = Array.from(switcher.querySelectorAll('.nora-company-options a')).find(function (link) {
            return link.dataset.companyKey === current;
        });
        try {
            if (ownLink && ownLink.dataset.companyTabs.split(',').includes(active)) {
                localStorage.setItem(prefix + current, active);
            }
            switcher.querySelectorAll('.nora-company-options a').forEach(function (link) {
                var saved = localStorage.getItem(prefix + link.dataset.companyKey);
                if (!saved || !link.dataset.companyTabs.split(',').includes(saved)) return;
                var target = new URL(link.href);
                target.searchParams.set('tab', saved);
                link.href = target.href;
            });
        } catch (e) { /* With storage unavailable, server-provided first-page links still work. */ }
    });

    var themeButton = document.getElementById('nora-theme');
    if (themeButton) {
        function updateThemeButton() {
            var current = typeof window.getDashboardTheme === 'function' ? window.getDashboardTheme() : 'light';
            var brandedCompany = document.querySelector('.nora-company-switcher > summary .nora-company-logo') !== null;
            var next = current === 'light' ? 'dark' : current === 'dark' && brandedCompany ? 'Nora Mart' : 'light';
            var label = 'Switch to ' + next + ' mode';
            themeButton.setAttribute('aria-label', label);
            themeButton.title = label;
            themeButton.innerHTML = '<iconify-icon icon="solar:' + (next === 'dark' ? 'moon' : next === 'Nora Mart' ? 'palette' : 'sun-2') + '-linear" width="19" aria-hidden="true"></iconify-icon>';
        }
        themeButton.addEventListener('click', function () {
            if (typeof window.cycleDashboardTheme === 'function') window.cycleDashboardTheme();
            updateThemeButton();
        });
        window.addEventListener('themeChange', updateThemeButton);
        updateThemeButton();
    }

    if (menu) menu.addEventListener('click', function () {
        if (mobileSidebar.classList.contains('is-open')) closeMobile();
        else openMobile();
    });
    if (scrim) scrim.addEventListener('click', closeMobile);
    if (mobileSidebar) mobileSidebar.querySelectorAll('a[href]').forEach(function (link) {
        link.addEventListener('click', closeMobile);
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && mobileSidebar && mobileSidebar.classList.contains('is-open')) {
            closeMobile();
            if (menu) menu.focus();
        }
    });
    document.querySelectorAll('.nora-open-dialog').forEach(function (button) {
        button.addEventListener('click', function () {
            var dialog = document.getElementById(button.dataset.dialog);
            if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
        });
    });
    document.querySelectorAll('.nora-schedule-dialog').forEach(function (dialog) {
        var close = dialog.querySelector('.nora-close-dialog');
        if (close) close.addEventListener('click', function () { dialog.close(); });
        dialog.addEventListener('click', function (event) { if (event.target === dialog) dialog.close(); });
    });
    window.addEventListener('resize', function () {
        if (window.innerWidth > 700) closeMobile();
    });
}());
