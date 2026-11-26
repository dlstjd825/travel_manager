(() => {
  const nav = document.querySelector('.trip-module-nav');
  if (!nav) return;
  const links = [...nav.querySelectorAll('a')];
  const activeIndex = links.findIndex((link) => link.classList.contains('active'));
  if (activeIndex < 0) return;

  const positionIndicator = (index) => {
    const link = links[index];
    if (!link) return;
    nav.style.setProperty('--nav-indicator-x', `${link.offsetLeft}px`);
    nav.style.setProperty('--nav-indicator-width', `${link.offsetWidth}px`);
  };

  positionIndicator(activeIndex);

  window.addEventListener('resize', () => positionIndicator(activeIndex));
})();
