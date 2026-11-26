const accommodationForm = document.getElementById('accommodation-form');
const accommodationSearchForm = document.getElementById('accommodation-search-form');
const accommodationEditor = document.getElementById('accommodation-editor');
const addAccommodationButton = document.getElementById('add-accommodation');
const setAccommodationEditorOpen = (isOpen) => {
  window.setAnimatedDisclosure(accommodationEditor, isOpen, addAccommodationButton);
  if (addAccommodationButton) {
    addAccommodationButton.textContent = isOpen ? '닫기' : '숙소 추가';
    addAccommodationButton.setAttribute('aria-expanded', String(isOpen));
  }
};

setAccommodationEditorOpen(Boolean(accommodationEditor && !accommodationEditor.hidden));

if (accommodationForm) {
  const checkInInput = accommodationForm.elements.check_in_date;
  const checkOutInput = accommodationForm.elements.check_out_date;
  const nameInput = accommodationForm.elements.name;
  const addressInput = accommodationForm.elements.address;
  const syncCheckoutDate = () => {
    checkOutInput.min = checkInInput.value || checkInInput.min;
    if (checkOutInput.value && checkOutInput.value <= checkInInput.value) checkOutInput.value = '';
  };
  checkInInput.addEventListener('change', syncCheckoutDate);
  syncCheckoutDate();

  const enableEntryFields = () => {
    [nameInput, addressInput, checkInInput, checkOutInput].forEach((field) => { field.disabled = false; });
    accommodationForm.querySelector('button[type="submit"]').disabled = false;
  };
  document.querySelectorAll('.accommodation-edit').forEach((button) => {
    button.addEventListener('click', () => {
      accommodationForm.elements.accommodation_id.value = button.dataset.id;
      accommodationForm.elements.name.value = button.dataset.name;
      accommodationForm.elements.address.value = button.dataset.address;
      checkInInput.value = button.dataset.checkIn;
      checkOutInput.value = button.dataset.checkOut;
      accommodationForm.elements.latitude.value = button.dataset.lat;
      accommodationForm.elements.longitude.value = button.dataset.lng;
      accommodationForm.elements.google_place_id.value = button.dataset.placeId;
      if (accommodationForm.elements.region_id) accommodationForm.elements.region_id.value = button.dataset.regionId;
      enableEntryFields();
      accommodationForm.querySelector('button[type="submit"]').textContent = '변경 저장';
      if (accommodationSearchForm) accommodationSearchForm.elements.accommodation_id.value = button.dataset.id;
      setAccommodationEditorOpen(true);
      accommodationForm.scrollIntoView({behavior: 'smooth', block: 'center'});
    });
  });

  addAccommodationButton?.addEventListener('click', () => {
    if (accommodationEditor?.classList.contains('is-expanded')) {
      setAccommodationEditorOpen(false);
      return;
    }
    accommodationForm.reset();
    accommodationForm.elements.accommodation_id.value = '';
    accommodationForm.elements.google_place_id.value = '';
    accommodationForm.elements.latitude.value = '';
    accommodationForm.elements.longitude.value = '';
    if (accommodationForm.elements.region_id) accommodationForm.elements.region_id.value = '';
    if (accommodationSearchForm) accommodationSearchForm.elements.accommodation_id.value = '';
    accommodationForm.querySelector('button[type="submit"]').textContent = '숙소 저장';
    setAccommodationEditorOpen(true);
    [nameInput, addressInput, checkInInput, checkOutInput].forEach((field) => { field.disabled = true; });
    accommodationForm.querySelector('button[type="submit"]').disabled = true;
    accommodationForm.scrollIntoView({behavior: 'smooth', block: 'center'});
  });
}

document.querySelectorAll('.select-location').forEach((button) => {
  button.addEventListener('click', () => {
    const form = document.getElementById(button.dataset.form);
    form.elements.latitude.value = button.dataset.lat;
    form.elements.longitude.value = button.dataset.lng;
    form.elements.google_place_id.value = button.dataset.placeId;
    form.elements.name.value = button.dataset.name;
    form.elements.address.value = button.dataset.address;
    [form.elements.name, form.elements.address, form.elements.check_in_date, form.elements.check_out_date]
      .forEach((field) => { field.disabled = false; });
    form.querySelector('button[type="submit"]').disabled = false;
    form.querySelector('button[type="submit"]').textContent = form.elements.accommodation_id.value ? '변경 저장' : '숙소 저장';
    setAccommodationEditorOpen(true);
    form.scrollIntoView({behavior: 'smooth', block: 'center'});
    form.elements.name.focus();
  });
});
