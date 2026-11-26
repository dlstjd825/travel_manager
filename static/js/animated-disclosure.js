window.setAnimatedDisclosure = (panel, isOpen, trigger) => {
  if (!panel) return;
  if (panel._disclosureTransition) {
    panel.removeEventListener('transitionend', panel._disclosureTransition);
    panel._disclosureTransition = null;
  }

  const wasOpen = panel.classList.contains('is-expanded');
  if (trigger) trigger.setAttribute('aria-expanded', String(isOpen));
  if (isOpen === wasOpen) {
    panel.hidden = !isOpen;
    return;
  }

  if (isOpen) {
    panel.hidden = false;
    panel.style.maxHeight = '0px';
    panel.style.opacity = '0';
    void panel.offsetHeight;
    panel.classList.add('is-expanded');
  } else {
    panel.style.maxHeight = `${panel.getBoundingClientRect().height}px`;
    panel.style.opacity = getComputedStyle(panel).opacity;
    void panel.offsetHeight;
    panel.classList.remove('is-expanded');
  }

  const onTransitionEnd = (event) => {
    if (event.target !== panel || event.propertyName !== 'max-height') return;
    panel.removeEventListener('transitionend', onTransitionEnd);
    panel._disclosureTransition = null;
    if (panel.classList.contains('is-expanded')) {
      panel.style.maxHeight = 'none';
      panel.style.opacity = '1';
    } else {
      panel.hidden = true;
      panel.style.maxHeight = '';
      panel.style.opacity = '';
    }
  };
  panel._disclosureTransition = onTransitionEnd;
  panel.addEventListener('transitionend', onTransitionEnd);

  if (isOpen) {
    requestAnimationFrame(() => {
      if (panel.classList.contains('is-expanded')) panel.style.maxHeight = `${panel.scrollHeight}px`;
      panel.style.opacity = '1';
    });
  } else {
    panel.style.maxHeight = '0px';
    panel.style.opacity = '0';
  }
};
