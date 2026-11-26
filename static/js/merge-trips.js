(() => {
  const dataElement = document.getElementById("merge-preview-data");
  const preview = document.getElementById("merge-preview");
  const summary = document.getElementById("merge-preview-summary");
  const hint = document.getElementById("merge-preview-hint");
  const daysContainer = document.getElementById("merge-preview-days");
  const transferDateList = document.getElementById("merge-transfer-date-list");
  const submitButton = document.getElementById("merge-submit");
  const checkboxes = [...document.querySelectorAll('input[name="trip_ids"]')];
  if (!dataElement || !preview || !summary || !hint || !daysContainer || !transferDateList || !submitButton) return;

  const trips = JSON.parse(dataElement.textContent);
  const selectedTransferDates = new Map();
  const regionLabel = (trip) => {
    const input = document.querySelector(`.merge-region-label[data-trip-id="${trip.id}"]`);
    return input?.value.trim() || trip.city;
  };
  const formatDate = (value) => new Intl.DateTimeFormat("ko-KR", {
    month: "long", day: "numeric", weekday: "short", timeZone: "UTC",
  }).format(new Date(`${value}T00:00:00Z`));
  const makeElement = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  };

  function findAccommodationTransfers(selectedTrips) {
    const candidates = [];
    const seen = new Set();
    selectedTrips.forEach((fromTrip) => selectedTrips.forEach((toTrip) => {
      if (fromTrip.id === toTrip.id) return;
      if (regionLabel(fromTrip).replace(/\s+/g, " ").toLocaleLowerCase() === regionLabel(toTrip).replace(/\s+/g, " ").toLocaleLowerCase()) return;
      (fromTrip.accommodations || []).forEach((fromStay) => (toTrip.accommodations || []).forEach((toStay) => {
        if (fromStay.check_out_date && fromStay.check_out_date === toStay.check_in_date) {
          const key = `${fromTrip.id}:${toTrip.id}:${fromStay.check_out_date}`;
          if (seen.has(key)) return;
          seen.add(key);
          candidates.push({ fromTrip, toTrip, fromStay, toStay, defaultDate: fromStay.check_out_date });
        }
      }));
    }));
    return candidates;
  }

  function renderPreview() {
    const selectedIds = new Set(checkboxes.filter((checkbox) => checkbox.checked).map((checkbox) => Number(checkbox.value)));
    const selectedTrips = trips.filter((trip) => selectedIds.has(Number(trip.id)));
    submitButton.disabled = selectedTrips.length < 2;
    preview.hidden = selectedTrips.length === 0;
    daysContainer.replaceChildren();
    transferDateList.replaceChildren();
    if (selectedTrips.length === 0) return;

    const firstDate = selectedTrips.map((trip) => trip.start_date).sort()[0];
    const lastDate = selectedTrips.map((trip) => trip.end_date).sort().at(-1);
    const start = new Date(`${firstDate}T00:00:00Z`);
    const end = new Date(`${lastDate}T00:00:00Z`);
    const totalDays = Math.round((end - start) / 86400000) + 1;
    if (selectedTrips.length < 2) {
      summary.textContent = "미리보려면 여행을 하나 더 선택해 주세요.";
      hint.textContent = "";
      return;
    }

    let overlapDays = 0;
    const transferCandidates = findAccommodationTransfers(selectedTrips);
    if (transferCandidates.length) {
      transferDateList.append(makeElement("h4", "", "국가 간 이동 날짜"));
      transferCandidates.forEach(({ fromTrip, toTrip, fromStay, toStay, defaultDate }) => {
        const fieldName = `transfer_date_${fromStay.id}_${toStay.id}`;
        const input = makeElement("input", "merge-transfer-date-input");
        input.type = "date";
        input.name = fieldName;
        input.min = firstDate;
        input.max = lastDate;
        input.required = true;
        input.value = selectedTransferDates.get(fieldName) || defaultDate;
        const row = makeElement("label", "merge-transfer-date-row");
        const caption = makeElement("span", "", `${regionLabel(fromTrip)} → ${regionLabel(toTrip)} · ${fromStay.name} 체크아웃 / ${toStay.name} 체크인`);
        row.append(caption, input);
        transferDateList.append(row);
        selectedTransferDates.set(fieldName, input.value);
        input.addEventListener("change", () => {
          selectedTransferDates.set(fieldName, input.value || defaultDate);
          renderPreview();
        });
      });
    } else {
      transferDateList.append(makeElement("p", "merge-preview-empty", "체크아웃과 체크인이 같은 날인 다른 국가 숙소가 없어 국가 이동 일정은 자동으로 추가되지 않습니다."));
    }
    const transferDatesByDate = new Map();
    transferCandidates.forEach(({ fromTrip, toTrip, fromStay, toStay, defaultDate }) => {
      const selectedDate = selectedTransferDates.get(`transfer_date_${fromStay.id}_${toStay.id}`) || defaultDate;
      const entry = transferDatesByDate.get(selectedDate) || [];
      entry.push(`${regionLabel(fromTrip)} → ${regionLabel(toTrip)}`);
      transferDatesByDate.set(selectedDate, entry);
    });
    const movementDays = transferDatesByDate.size;
    let duplicatePlaces = 0;
    for (let offset = 0; offset < totalDays; offset += 1) {
      const dayDate = new Date(start.getTime() + offset * 86400000).toISOString().slice(0, 10);
      const activeTrips = selectedTrips.filter((trip) => trip.start_date <= dayDate && trip.end_date >= dayDate);
      const dayEntries = activeTrips.map((trip) => ({
        trip,
        day: trip.days.find((item) => item.date === dayDate),
        places: (trip.days.find((item) => item.date === dayDate)?.places || []),
      }));
      const movements = transferDatesByDate.get(dayDate) || [];
      const providerCounts = new Map();
      dayEntries.forEach(({ places }) => places.forEach((place) => {
        if (place.provider_id) providerCounts.set(place.provider_id, (providerCounts.get(place.provider_id) || 0) + 1);
      }));
      const dayDuplicates = [...providerCounts.values()].filter((count) => count > 1).length;
      duplicatePlaces += dayDuplicates;
      if (activeTrips.length > 1) overlapDays += 1;

      const dayCard = makeElement("article", "merge-preview-day");
      if (activeTrips.length > 1) dayCard.classList.add("is-overlap");
      const heading = makeElement("div", "merge-preview-day-heading");
      heading.append(
        makeElement("strong", "", `DAY ${offset + 1}`),
        makeElement("span", "", formatDate(dayDate)),
      );
      if (activeTrips.length > 1) heading.append(makeElement("span", "merge-preview-badge", "기간 겹침"));
      if (movements.length) heading.append(makeElement("span", "merge-preview-badge is-movement", `국가 이동 · ${movements.join(", ")}`));
      if (dayDuplicates) heading.append(makeElement("span", "merge-preview-badge is-duplicate", `중복 장소 ${dayDuplicates}곳`));
      dayCard.append(heading);

      if (dayEntries.length === 0) {
        dayCard.append(makeElement("p", "merge-preview-empty", "선택한 여행 사이의 빈 날짜"));
      } else {
        dayEntries.forEach(({ trip, places }) => {
          const source = makeElement("div", "merge-preview-source");
          source.append(makeElement("strong", "", `${trip.title} · ${regionLabel(trip)}`));
          if (places.length) {
            const list = makeElement("ul", "merge-preview-place-list");
            places.forEach((place) => {
              const item = makeElement("li", "", place.name);
              if (place.provider_id && providerCounts.get(place.provider_id) > 1) {
                item.append(makeElement("small", "merge-preview-duplicate-label", "중복"));
              }
              list.append(item);
            });
            source.append(list);
          } else {
            source.append(makeElement("p", "merge-preview-empty", "등록된 방문 일정 없음"));
          }
          dayCard.append(source);
        });
      }
      daysContainer.append(dayCard);
    }

    summary.textContent = `${selectedTrips.length}개 여행 · ${firstDate} — ${lastDate} · ${totalDays}일 · 국가 이동 ${movementDays}일`;
    const notes = [];
    notes.push(overlapDays
      ? `${overlapDays}일의 여행 기간이 겹칩니다. 해당 날짜 일정은 여행 시작일 순서로 이어집니다.`
      : "선택한 여행의 날짜가 겹치지 않습니다.");
    if (duplicatePlaces) notes.push(`같은 날짜에 중복된 장소 ${duplicatePlaces}곳은 병합 시 한 번만 일정에 들어갑니다.`);
    if (movementDays) notes.push("국가 이동은 숙소 체크아웃·체크인이 겹치는 날짜를 기준으로 추가되며, 위에서 날짜를 바꿀 수 있습니다.");
    hint.textContent = notes.join(" ");
  }

  checkboxes.forEach((checkbox) => checkbox.addEventListener("change", renderPreview));
  document.querySelectorAll(".merge-region-label").forEach((input) => input.addEventListener("input", renderPreview));
  renderPreview();
})();
