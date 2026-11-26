const transportEditor = document.getElementById('transport-editor');
const transportToggle = document.getElementById('toggle-transport-form');

transportToggle?.addEventListener('click', () => {
  const isOpen = !transportEditor.classList.contains('is-expanded');
  window.setAnimatedDisclosure(transportEditor, isOpen, transportToggle);
  transportToggle.textContent = isOpen ? '닫기' : '교통편 추가';
  if (isOpen) transportEditor.scrollIntoView({behavior: 'smooth', block: 'start'});
});
