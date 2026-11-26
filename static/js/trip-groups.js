document.querySelectorAll('.trip-children-details[data-trip-id]').forEach((group) => {
  const storageKey = `trip-manager:merged-trip:${group.dataset.tripId}:sources-open`;

  try {
    const savedState = localStorage.getItem(storageKey);
    if (savedState !== null) group.open = savedState === 'true';
  } catch (_error) {
    // Keep the default expanded state when browser storage is unavailable.
  }

  group.addEventListener('toggle', () => {
    try {
      localStorage.setItem(storageKey, String(group.open));
    } catch (_error) {
      // The disclosure still works for the current page if storage is unavailable.
    }
  });
});
