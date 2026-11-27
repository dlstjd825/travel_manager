
    let zones = [...document.querySelectorAll('.day-dropzone')];
    const dayList = document.querySelector('.day-list');
    const reorderUrl = dayList.dataset.reorderUrl;
    const calendarDates = zones.map((zone) => zone.dataset.date);
    const statusNode = document.getElementById('itinerary-save-status');
    let draggedItem = null;
    let draggedDayCard = null;
    let previousLayout = null;
    let previousDayOrder = null;
    let previousDayMeta = null;

    function refreshZones() {
      zones = [...document.querySelectorAll('.day-dropzone')];
    }

    function currentLayout() {
      return zones.map((zone) => ({
        zone,
        placeIds: [...zone.querySelectorAll('.itinerary-stop')].map((item) => item.dataset.placeId),
      }));
    }

    function takeSnapshot() {
      refreshZones();
      previousLayout = currentLayout();
      previousDayOrder = [...dayList.querySelectorAll('.day-card')];
      previousDayMeta = zones.map((zone) => ({
        zone,
        dayNumber: zone.dataset.dayNumber,
        date: zone.dataset.date,
        detailUrl: zone.dataset.detailUrl,
      }));
    }

    function reindexDayCards() {
      refreshZones();
      zones.forEach((zone, index) => {
        const dayNumber = index + 1;
        const dayDate = calendarDates[index];
        zone.dataset.dayNumber = String(dayNumber);
        zone.dataset.date = dayDate;
        zone.dataset.detailUrl = zone.dataset.detailUrl.replace(/\/day\/\d+$/, `/day/${dayNumber}`);
        const card = zone.closest('.day-card');
        card.querySelector('.day-badge strong').textContent = String(dayNumber).padStart(2, '0');
        card.querySelector('.day-heading h2').textContent = dayDate;
        updateDayDetailLink(zone);
      });
      updateEmptyDays();
    }

    function updateEmptyDays() {
      zones.forEach((zone) => {
        const empty = zone.querySelector('.free-day');
        empty.hidden = zone.querySelectorAll('.itinerary-stop').length > 0;
      });
    }

    function updateDayDetailLink(zone) {
      const card = zone.closest('.day-card');
      const link = card.querySelector('.day-detail-link');
      if (link) link.href = zone.dataset.detailUrl;
    }

    function placeBeforeElement(list, y) {
      const others = [...list.querySelectorAll('.itinerary-stop:not(.dragging)')];
      return others.reduce((closest, item) => {
        const box = item.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) return {offset, item};
        return closest;
      }, {offset: Number.NEGATIVE_INFINITY, item: null}).item;
    }

    async function saveLayout() {
      statusNode.textContent = '일정을 저장하고 있어요…';
      const days = zones.map((zone) => ({
        day_number: Number(zone.dataset.dayNumber),
        place_ids: [...zone.querySelectorAll('.itinerary-stop')].map((item) => Number(item.dataset.placeId)),
      }));
      const timeline = zones.map((zone) => ({
        day_number: Number(zone.dataset.dayNumber),
        event_keys: [...zone.querySelector('.itinerary-stops').children]
          .map((item) => item.dataset.eventKey)
          .filter(Boolean),
      }));
      try {
        const response = await fetch(reorderUrl, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({days, timeline}),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || '저장하지 못했습니다.');
        if (window.updateItineraryMapDays) {
          const dayAssignments = {};
          days.forEach(({day_number, place_ids}) => place_ids.forEach((id, index) => {
            dayAssignments[id] = {day_number, sequence_number: index + 1};
          }));
          window.updateItineraryMapDays(dayAssignments);
        }
        zones.forEach((zone) => {
          const heading = zone.closest('.day-card').querySelector('.day-heading p');
          const itemCount = zone.querySelectorAll('.itinerary-stop').length;
          heading.textContent = itemCount ? `${itemCount}곳 방문` : '일정 여유일';
        });
        statusNode.textContent = '일정을 저장했어요.';
      } catch (error) {
        if (previousDayOrder) previousDayOrder.forEach((card) => dayList.append(card));
        if (previousDayMeta) previousDayMeta.forEach(({zone, dayNumber, date, detailUrl}) => {
          zone.dataset.dayNumber = dayNumber;
          zone.dataset.date = date;
          zone.dataset.detailUrl = detailUrl;
          const card = zone.closest('.day-card');
          card.querySelector('.day-badge strong').textContent = String(dayNumber).padStart(2, '0');
          card.querySelector('.day-heading h2').textContent = date;
          const link = card.querySelector('.day-detail-link');
          if (link) link.href = detailUrl;
        });
        refreshZones();
        previousLayout.forEach(({zone, placeIds}) => {
          const list = zone.querySelector('.itinerary-stops');
          placeIds.forEach((id) => {
            const item = document.querySelector(`.itinerary-stop[data-place-id="${id}"]`);
            if (item) list.append(item);
          });
        });
        updateEmptyDays();
        statusNode.textContent = error.message;
      }
    }

    document.querySelectorAll('.itinerary-stop').forEach((item) => {
      item.addEventListener('dragstart', (event) => {
        if (item.classList.contains('is-locked')) {
          event.preventDefault();
          return;
        }
        draggedItem = item;
        event.stopPropagation();
        takeSnapshot();
        item.classList.add('dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', item.dataset.placeId);
      });
      item.addEventListener('dragend', () => item.classList.remove('dragging'));
    });

    zones.forEach((zone) => {
      const list = zone.querySelector('.itinerary-stops');
      zone.addEventListener('dragover', (event) => {
        if (!draggedItem) return;
        event.preventDefault();
        zone.classList.add('drag-over');
        const after = placeBeforeElement(list, event.clientY);
        if (after) list.insertBefore(draggedItem, after);
        else list.append(draggedItem);
      });
      zone.addEventListener('dragleave', (event) => {
        if (!zone.contains(event.relatedTarget)) zone.classList.remove('drag-over');
      });
      zone.addEventListener('drop', async (event) => {
        if (!draggedItem) return;
        event.preventDefault();
        zone.classList.remove('drag-over');
        draggedItem.classList.remove('dragging');
        draggedItem = null;
        updateEmptyDays();
        await saveLayout();
      });
    });

    document.querySelectorAll('.day-drag-handle').forEach((handle) => {
      let pointerStart = null;
      let touchMoved = false;
      handle.addEventListener('pointerdown', (event) => {
        if (event.pointerType !== 'touch') return;
        pointerStart = {x: event.clientX, y: event.clientY};
        touchMoved = false;
        draggedDayCard = handle.closest('.day-card');
        takeSnapshot();
      });
      handle.addEventListener('pointermove', (event) => {
        if (!pointerStart || !draggedDayCard || event.pointerType !== 'touch') return;
        if (Math.abs(event.clientY - pointerStart.y) < 8) return;
        touchMoved = true;
        const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('.day-card');
        if (target && target !== draggedDayCard) {
          const box = target.getBoundingClientRect();
          dayList.insertBefore(draggedDayCard, event.clientY > box.top + box.height / 2 ? target.nextElementSibling : target);
        }
      });
      handle.addEventListener('pointerup', async (event) => {
        if (!pointerStart || event.pointerType !== 'touch') return;
        pointerStart = null;
        if (touchMoved && draggedDayCard) {
          draggedDayCard = null;
          reindexDayCards();
          await saveLayout();
        } else draggedDayCard = null;
      });
      handle.addEventListener('dragstart', (event) => {
        event.stopPropagation();
        takeSnapshot();
        draggedDayCard = handle.closest('.day-card');
        draggedDayCard.classList.add('day-card-dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', handle.closest('.day-dropzone').dataset.dayNumber);
      });
      handle.addEventListener('dragend', () => {
        if (draggedDayCard) draggedDayCard.classList.remove('day-card-dragging');
        draggedDayCard = null;
      });
    });

    document.querySelectorAll('.day-list .day-card').forEach((card) => {
      card.addEventListener('dragover', (event) => {
        if (!draggedDayCard) return;
        event.preventDefault();
        event.stopPropagation();
        if (card === draggedDayCard) return;
        const box = card.getBoundingClientRect();
        const insertAfter = event.clientY >= box.top + box.height / 2;
        dayList.insertBefore(draggedDayCard, insertAfter ? card.nextElementSibling : card);
      });
      card.addEventListener('drop', async (event) => {
        if (!draggedDayCard) return;
        event.preventDefault();
        event.stopPropagation();
        draggedDayCard.classList.remove('day-card-dragging');
        draggedDayCard = null;
        reindexDayCards();
        await saveLayout();
      });
    });
    dayList.addEventListener('dragover', (event) => {
      if (!draggedDayCard) return;
      event.preventDefault();
    });
    dayList.addEventListener('drop', async (event) => {
      if (!draggedDayCard) return;
      event.preventDefault();
      dayList.append(draggedDayCard);
      draggedDayCard.classList.remove('day-card-dragging');
      draggedDayCard = null;
      reindexDayCards();
      await saveLayout();
    });
