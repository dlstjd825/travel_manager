(() => {
  const dayCards = [...document.querySelectorAll('.day-card[data-day-number]')];
  const mapDayButtons = [...document.querySelectorAll('.map-day-filter')];
  const mapDaySelect = document.getElementById('map-day-select');
  const expandAllButton = document.getElementById('itinerary-expand-all');
  const itineraryLayout = document.querySelector('.itinerary-layout');
  const scheduleColumn = document.querySelector('.itinerary-schedule-column');
  const mapPanel = document.querySelector('.itinerary-map-panel');
  const openDaysStorageKey = `trip-manager:itinerary-open:${location.pathname}${location.search}`;
  let savedOpenDays = null;
  try {
    const storedOpenDays = sessionStorage.getItem(openDaysStorageKey);
    if (storedOpenDays !== null) savedOpenDays = JSON.parse(storedOpenDays);
    if (!Array.isArray(savedOpenDays)) savedOpenDays = null;
  } catch (_) {
    savedOpenDays = null;
  }

  function saveOpenDayState() {
    const openDays = dayCards
      .filter((card) => card.classList.contains('is-open'))
      .map((card) => card.dataset.dayNumber);
    try { sessionStorage.setItem(openDaysStorageKey, JSON.stringify(openDays)); } catch (_) { /* Storage can be unavailable in private browsing. */ }
  }

  function alignMapWithFirstDay() {
    const firstDayCard = document.querySelector('.day-card[data-day-number]');
    if (!itineraryLayout || !firstDayCard || !mapPanel) return;
    if (!window.matchMedia('(min-width: 901px)').matches) {
      mapPanel.style.marginTop = '';
      return;
    }
    const layoutTop = itineraryLayout.getBoundingClientRect().top;
    const dayTop = firstDayCard.getBoundingClientRect().top;
    mapPanel.style.marginTop = `${Math.max(0, dayTop - layoutTop)}px`;
  }

  function setMapDay(day) {
    window.selectedItineraryMapDay = day;
    mapDayButtons.forEach((button) => {
      const active = button.dataset.day === day;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    if (mapDaySelect) mapDaySelect.value = day === 'all' ? '' : String(day);
    if (typeof window.setItineraryMapDay === 'function') window.setItineraryMapDay(day);
  }

  function setCardOpen(card, isOpen, animate = true) {
    const content = card.querySelector('.day-dropzone');
    const toggle = card.querySelector('.day-card-toggle');
    if (!content || !toggle) return;
    if (content._dayToggleTransition) {
      content.removeEventListener('transitionend', content._dayToggleTransition);
      content._dayToggleTransition = null;
    }
    const wasOpen = card.classList.contains('is-open');
    if (isOpen && !wasOpen) {
      content.hidden = false;
      if (animate) {
        content.style.maxHeight = '0px';
        content.style.minHeight = '0px';
        content.style.opacity = '0';
        void content.offsetHeight;
      }
    } else if (!isOpen && wasOpen && animate) {
      content.style.maxHeight = `${content.getBoundingClientRect().height}px`;
      content.style.minHeight = '0px';
      content.style.opacity = getComputedStyle(content).opacity;
      void content.offsetHeight;
    }
    card.classList.toggle('is-open', isOpen);
    toggle.setAttribute('aria-expanded', String(isOpen));
    toggle.setAttribute(
      'aria-label',
      `Day ${card.dataset.dayNumber} 일정 ${isOpen ? '접기' : '펼치기'}`,
    );
    toggle.textContent = '⌄';

    if (!animate || isOpen === wasOpen) {
      content.hidden = !isOpen;
      content.style.maxHeight = isOpen ? 'none' : '';
      content.style.opacity = isOpen ? '1' : '';
      return;
    }

    const handleTransitionEnd = (event) => {
      if (event.target !== content || event.propertyName !== 'max-height') return;
      content.removeEventListener('transitionend', handleTransitionEnd);
      content._dayToggleTransition = null;
      if (card.classList.contains('is-open')) {
        content.style.maxHeight = 'none';
        const hasStops = content.querySelector('.itinerary-stop, .itinerary-transfer-step, .day-accommodations');
        content.style.minHeight = hasStops ? '' : '100px';
        content.style.opacity = '1';
      } else {
        content.hidden = true;
        content.style.maxHeight = '';
        content.style.minHeight = '';
        content.style.opacity = '';
      }
    };
    content._dayToggleTransition = handleTransitionEnd;
    content.addEventListener('transitionend', handleTransitionEnd);
    if (isOpen) {
      requestAnimationFrame(() => {
        if (card.classList.contains('is-open')) content.style.maxHeight = `${Math.max(content.scrollHeight, 100)}px`;
        content.style.opacity = '1';
      });
    } else {
      content.style.maxHeight = '0px';
      content.style.opacity = '0';
    }
  }

  function syncExpandAllButton() {
    if (!expandAllButton) return;
    const allOpen = dayCards.length > 0 && dayCards.every((card) => card.classList.contains('is-open'));
    expandAllButton.setAttribute('aria-pressed', String(allOpen));
    expandAllButton.textContent = allOpen ? '모든 일정 접기' : '모든 일정 펼치기';
  }

  function highlightPlace(placeId, scroll = true) {
    const row = [...document.querySelectorAll('[data-place-id]')]
      .find((element) => element.dataset.placeId === String(placeId));
    if (!row) return;
    row.classList.remove('map-focused');
    if (scroll) row.scrollIntoView({behavior: 'smooth', block: 'center'});
    requestAnimationFrame(() => row.classList.add('map-focused'));
    window.setTimeout(() => row.classList.remove('map-focused'), 1800);
  }

  function focusPlaceFromMap({placeId, dayNumber}) {
    if (dayNumber) {
      const card = dayCards.find((item) => item.dataset.dayNumber === String(dayNumber));
      if (card) setCardOpen(card, true);
      setMapDay(String(dayNumber));
      syncExpandAllButton();
    } else {
      setMapDay('all');
    }
    highlightPlace(placeId);
  }

  dayCards.forEach((card) => {
    const toggle = card.querySelector('.day-card-toggle');
    const content = card.querySelector('.day-dropzone');
    if (!toggle || !content) return;
    if (savedOpenDays !== null) content.hidden = !savedOpenDays.includes(card.dataset.dayNumber);
    setCardOpen(card, !content.hidden, false);
    toggle.addEventListener('click', () => {
      const isOpen = !card.classList.contains('is-open');
      setCardOpen(card, isOpen);
      saveOpenDayState();
      const openCards = dayCards.filter((dayCard) => dayCard.classList.contains('is-open'));
      setMapDay(openCards.length === 1 ? openCards[0].dataset.dayNumber : 'all');
      syncExpandAllButton();
    });
  });

  expandAllButton?.addEventListener('click', () => {
    const allOpen = dayCards.length > 0 && dayCards.every((card) => card.classList.contains('is-open'));
    dayCards.forEach((card) => setCardOpen(card, !allOpen));
    saveOpenDayState();
    setMapDay('all');
    syncExpandAllButton();
  });

  mapDayButtons.forEach((button) => {
    button.addEventListener('click', () => setMapDay(button.dataset.day));
  });
  mapDaySelect?.addEventListener('change', () => setMapDay(mapDaySelect.value || 'all'));

  document.querySelectorAll('.itinerary-stop, .unscheduled-place-row').forEach((row) => {
    row.addEventListener('click', (event) => {
      if (event.target.closest('a, button, form, select, input, textarea, label')) return;
      const placeId = row.dataset.placeId;
      if (!placeId) return;
      highlightPlace(placeId, false);
      if (typeof window.focusItineraryMapPlace === 'function') window.focusItineraryMapPlace(placeId);
    });
  });
  window.addEventListener('itinerary-map-place-selected', (event) => {
    if (event.detail?.placeId) {
      focusPlaceFromMap(event.detail);
      saveOpenDayState();
    }
  });

  document.addEventListener('submit', (event) => {
    if (event.target instanceof HTMLFormElement) saveOpenDayState();
  }, true);
  window.addEventListener('pagehide', saveOpenDayState);

  syncExpandAllButton();
  setMapDay('all');
  requestAnimationFrame(alignMapWithFirstDay);
  window.addEventListener('resize', alignMapWithFirstDay);
  if (window.ResizeObserver && scheduleColumn) {
    const scheduleObserver = new ResizeObserver(alignMapWithFirstDay);
    scheduleObserver.observe(scheduleColumn);
  }
})();
