(() => {
  const config = document.getElementById('trip-sync-config');
  if (!config) return;
  const notice = document.getElementById('trip-live-notice');
  const url = config.dataset.revisionUrl;
  const stateKey = `trip-manager:live-state:${config.dataset.tripId}:${location.pathname}${location.search}`;
  let revision = null;
  let dirty = false;
  let pendingWrites = 0;
  document.addEventListener('input', () => { dirty = true; }, true);
  document.addEventListener('change', () => { dirty = true; }, true);
  document.addEventListener('dragstart', () => { dirty = true; }, true);
  const preserveViewAndReload = () => {
    sessionStorage.setItem(stateKey, JSON.stringify({
      savedAt: Date.now(), scrollX: window.scrollX, scrollY: window.scrollY,
      details: [...document.querySelectorAll('details')].map((node) => node.open),
      mapDay: document.querySelector('.map-day-filter.active')?.dataset.day || 'all',
    }));
    window.location.reload();
  };
  try {
    const saved = JSON.parse(sessionStorage.getItem(stateKey) || 'null');
    if (saved && Date.now() - saved.savedAt < 60000) {
      window.addEventListener('load', () => {
        window.scrollTo(saved.scrollX || 0, saved.scrollY || 0);
        document.querySelectorAll('details').forEach((node, index) => { node.open = Boolean(saved.details[index]); });
        const selected = document.querySelector(`.map-day-filter[data-day="${saved.mapDay}"]`);
        if (selected && !selected.classList.contains('active')) selected.click();
        sessionStorage.removeItem(stateKey);
      }, {once:true});
    }
  } catch (_) { sessionStorage.removeItem(stateKey); }
  document.getElementById('trip-live-refresh')?.addEventListener('click', preserveViewAndReload);
  fetch(url).then((r) => r.json()).then((data) => {
    revision = data.revision;
    document.querySelectorAll('form[method="post"], form[method="POST"]').forEach((form) => {
      if (!form.querySelector('[name="expected_revision"]')) {
        const input = document.createElement('input');
        input.type = 'hidden'; input.name = 'expected_revision'; input.value = revision;
        form.append(input);
      }
    });
  });
  let changed = false;
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const requestUrl = typeof input === 'string' ? input : input.url;
    const urlObject = new URL(requestUrl, window.location.href);
    const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (urlObject.origin === window.location.origin && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : {}));
      headers.set('X-CSRFToken', document.querySelector('[name="csrf_token"]')?.value || '');
      if (headers.get('Content-Type')?.includes('application/json') && typeof init.body === 'string') {
        try { const body = JSON.parse(init.body); body.expected_revision = revision; init.body = JSON.stringify(body); } catch (_) { }
      }
      init = {...init, headers};
    }
    const writing = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);
    if (writing) pendingWrites += 1;
    const response = await originalFetch(input, init);
    if (writing) {
      pendingWrites = Math.max(0, pendingWrites - 1);
      if (response.ok) {
        dirty = false;
        const savedRevision = response.headers.get('X-Trip-Revision');
        if (savedRevision !== null) {
          revision = Number(savedRevision);
          document.querySelectorAll('[name="expected_revision"]').forEach((field) => {
            field.value = String(revision);
          });
          changed = false;
          if (notice) notice.hidden = true;
        }
      }
    }
    return response;
  };
  setInterval(async () => {
    try {
      const response = await fetch(url, {cache: 'no-store'});
      const data = await response.json();
      if (revision !== null && data.revision !== revision) {
        changed = true;
        if (!dirty && pendingWrites === 0) {
          preserveViewAndReload();
          return;
        }
        if (notice) notice.hidden = false;
      }
      if (!changed) revision = data.revision;
    } catch (_) { /* Poll again on the next interval. */ }
  }, 1000);
})();
