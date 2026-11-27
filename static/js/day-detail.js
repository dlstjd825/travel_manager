const stopList = document.getElementById('day-stop-list');
const statusNode = document.getElementById('day-save-status');

if (stopList) {
  let draggedEvent = null;
  let originalNodes = null;
  let dropHandled = false;

  const timelineNodes = () => [...stopList.children].filter((node) => node.dataset.eventKey);
  const placeIds = () => [...stopList.querySelectorAll('.day-detail-stop')]
    .map((item) => Number(item.dataset.placeId));
  const eventKeys = () => timelineNodes().map((item) => item.dataset.eventKey);
  const refreshNumbers = () => stopList.querySelectorAll('.day-detail-stop').forEach((item, index) => {
    const number = item.querySelector('.day-stop-number');
    if (number) number.textContent = String(index + 1);
  });
  const restoreNodes = (nodes) => nodes.forEach((node) => stopList.append(node));

  const findBeforeNode = (pointerY) => timelineNodes().filter((node) => node !== draggedEvent).reduce((closest, node) => {
    const box = node.getBoundingClientRect();
    const offset = pointerY - box.top - box.height / 2;
    return offset < 0 && offset > closest.offset ? {offset, node} : closest;
  }, {offset: Number.NEGATIVE_INFINITY, node: null}).node;

  const moveDraggedEvent = (pointerY) => {
    const before = findBeforeNode(pointerY);
    if (before) stopList.insertBefore(draggedEvent, before);
    else stopList.append(draggedEvent);
    refreshNumbers();
  };

  const saveOrder = async (snapshot) => {
    const submittedPlaceIds = placeIds();
    statusNode.textContent = '일정 순서를 저장하고 있어요…';
    try {
      const response = await fetch(stopList.dataset.reorderUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({place_ids: submittedPlaceIds, event_keys: eventKeys()}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '순서를 저장하지 못했습니다.');
      if (window.updateDayMarkerOrder) window.updateDayMarkerOrder(submittedPlaceIds);
      statusNode.textContent = '일정 순서를 저장했어요.';
    } catch (error) {
      restoreNodes(snapshot);
      refreshNumbers();
      if (window.updateDayMarkerOrder) {
        window.updateDayMarkerOrder(snapshot
          .filter((node) => node.classList.contains('day-detail-stop'))
          .map((node) => Number(node.dataset.placeId)));
      }
      statusNode.textContent = error.message;
    }
  };

  timelineNodes().forEach((item) => {
    item.addEventListener('dragstart', (event) => {
      if (event.target.closest('form, details, input, select, textarea, button')) {
        event.preventDefault();
        return;
      }
      if (item.classList.contains('is-locked')) {
        event.preventDefault();
        return;
      }
      draggedEvent = item;
      originalNodes = timelineNodes();
      dropHandled = false;
      item.classList.add('timeline-dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', item.dataset.eventKey);
      const preview = document.createElement('div');
      preview.className = 'itinerary-drag-preview';
      preview.textContent = item.querySelector('.stop-details h3, .itinerary-stay-event strong')?.textContent.trim() || '일정';
      document.body.append(preview);
      event.dataTransfer.setDragImage(preview, 18, 18);
      window.setTimeout(() => preview.remove(), 0);
    });
    item.addEventListener('dragend', () => {
      item.classList.remove('timeline-dragging');
      if (!dropHandled && originalNodes) {
        restoreNodes(originalNodes);
        refreshNumbers();
      }
      draggedEvent = null;
      originalNodes = null;
      dropHandled = false;
    });

    const handle = item.querySelector('[data-timeline-drag-handle]');
    if (handle) {
      let pointerStartY = 0;
      let pointerStarted = false;
      let pointerSnapshot = null;

      handle.addEventListener('pointerdown', (event) => {
        if (event.button !== undefined && event.button !== 0) return;
        event.preventDefault();
        pointerStartY = event.clientY;
        pointerStarted = false;
        pointerSnapshot = null;
        handle.setPointerCapture(event.pointerId);
      });

      handle.addEventListener('pointermove', (event) => {
        if (!handle.hasPointerCapture(event.pointerId)) return;
        if (!pointerStarted && Math.abs(event.clientY - pointerStartY) < 6) return;
        if (!pointerStarted) {
          pointerStarted = true;
          pointerSnapshot = timelineNodes();
          draggedEvent = item;
          item.classList.add('timeline-dragging');
        }
        event.preventDefault();
        moveDraggedEvent(event.clientY);
      });

      const finishPointerDrag = async (event, cancelled = false) => {
        if (!handle.hasPointerCapture(event.pointerId)) return;
        handle.releasePointerCapture(event.pointerId);
        if (pointerStarted) {
          item.classList.remove('timeline-dragging');
          if (cancelled) {
            restoreNodes(pointerSnapshot);
            refreshNumbers();
          } else {
            await saveOrder(pointerSnapshot);
          }
          draggedEvent = null;
        }
        pointerStarted = false;
        pointerSnapshot = null;
      };

      handle.addEventListener('pointerup', (event) => finishPointerDrag(event));
      handle.addEventListener('pointercancel', (event) => finishPointerDrag(event, true));
    }
  });

  stopList.addEventListener('dragover', (event) => {
    if (!draggedEvent) return;
    event.preventDefault();
    moveDraggedEvent(event.clientY);
  });

  stopList.addEventListener('drop', async (event) => {
    if (!draggedEvent) return;
    event.preventDefault();
    event.stopPropagation();
    dropHandled = true;
    const snapshot = originalNodes;
    draggedEvent.classList.remove('timeline-dragging');
    draggedEvent = null;
    await saveOrder(snapshot);
  });
}
