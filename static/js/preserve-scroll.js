(() => {
  const storageKey = 'travel-manager:post-scroll-position';
  const saved = sessionStorage.getItem(storageKey);

  if (saved) {
    sessionStorage.removeItem(storageKey);
    try {
      const state = JSON.parse(saved);
      if (state.path === location.pathname && Date.now() - state.savedAt < 30000) {
        window.addEventListener('load', () => {
          requestAnimationFrame(() => window.scrollTo(state.x || 0, state.y || 0));
        }, { once: true });
      }
    } catch {
      // Ignore stale or malformed scroll state.
    }
  }

  document.addEventListener('submit', (event) => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;

    const destination = new URL(form.action || location.href, location.href);
    if (destination.origin !== location.origin) return;

    sessionStorage.setItem(storageKey, JSON.stringify({
      path: location.pathname,
      x: window.scrollX,
      y: window.scrollY,
      savedAt: Date.now(),
    }));
  });
})();
