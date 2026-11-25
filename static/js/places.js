const categoryButtons = document.querySelectorAll('.category-option');
const searchCategory = document.getElementById('search-category');
const formCategory = document.getElementById('place-form-category');
const searchLabel = document.getElementById('place-search-label');

categoryButtons.forEach((button) => {
  button.addEventListener('click', () => {
    categoryButtons.forEach((option) => option.classList.remove('active'));
    button.classList.add('active');

    const category = button.dataset.category;
    searchCategory.value = category;
    formCategory.value = category;
    searchLabel.textContent = category === 'restaurant' ? '식당 검색' : '관광지 검색';
  });
});

document.querySelectorAll('.select-location').forEach((button) => {
  button.addEventListener('click', () => {
    const form = document.getElementById('place-form');
    form.elements.latitude.value = button.dataset.lat;
    form.elements.longitude.value = button.dataset.lng;
    form.elements.provider_place_id.value = button.dataset.placeId;
    form.elements.category.value = button.dataset.category;
    form.elements.name.value = button.dataset.name;
    form.elements.address.value = button.dataset.address;

    form.querySelectorAll('input:not([type="hidden"]), button[type="submit"]')
      .forEach((field) => {
        field.disabled = false;
      });

    form.scrollIntoView({ behavior: 'smooth', block: 'center' });
    form.elements.name.focus();
  });
});
