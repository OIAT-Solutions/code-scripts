(function () {
    'use strict';
    let busy = false;
    async function refresh() {
        const root = document.getElementById('overview-panels-root');
        if (!root || busy || document.hidden) return;
        busy = true;
        try {
            const url = new URL(root.dataset.panelsUrl, window.location.origin);
            url.search = window.location.search;
            const response = await fetch(url, {cache: 'no-store', headers: {'X-Requested-With': 'XMLHttpRequest'}});
            if (response.ok) root.innerHTML = await response.text();
        } catch (_) { /* Keep the last checked time when a refresh fails. */ }
        finally { busy = false; }
    }
    window.addEventListener('oiat:run-completed', refresh);
    window.addEventListener('oiat:run-started', refresh);
    setInterval(() => { if (!document.querySelector('#overview-panels-root details[open]')) refresh(); }, 60000);
})();
