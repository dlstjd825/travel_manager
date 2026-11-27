const regionEditor = document.getElementById('trip-region-editor');
const addRegionButton = document.getElementById('add-trip-region');

addRegionButton?.addEventListener('click', () => {
  const isOpen = regionEditor?.classList.contains('is-expanded') || false;
  window.setAnimatedDisclosure(regionEditor, !isOpen, addRegionButton);
  addRegionButton.textContent = isOpen ? '국가 추가' : '닫기';
  if (!isOpen) regionEditor?.querySelector('input[name="label"]')?.focus({preventScroll: true});
});
