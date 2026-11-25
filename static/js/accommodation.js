const accommodationForm = document.getElementById('accommodation-form');

if (accommodationForm) {
  const checkInInput = accommodationForm.elements.check_in_date;
  const checkOutInput = accommodationForm.elements.check_out_date;
  const originalTripStart = checkOutInput.min;

  const syncCheckoutDate = () => {
    checkOutInput.min = checkInInput.value || originalTripStart;
    if (checkOutInput.value && checkOutInput.value <= checkInInput.value) {
      checkOutInput.value = '';
    }
  };

  checkInInput.addEventListener('change', syncCheckoutDate);
  syncCheckoutDate();
}

document.querySelectorAll('.select-location').forEach((button) => {
  button.addEventListener('click', () => {
    const form = document.getElementById(button.dataset.form);
    form.elements.latitude.value = button.dataset.lat;
    form.elements.longitude.value = button.dataset.lng;
    form.elements.google_place_id.value = button.dataset.placeId;
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
