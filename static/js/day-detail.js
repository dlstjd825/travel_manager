const stopList = document.getElementById('day-stop-list');
    const statusNode = document.getElementById('day-save-status');
    if (stopList) {
      let draggedStop = null;
      let previousOrder = [];
      const readOrder = () => [...stopList.querySelectorAll('.day-detail-stop')].map((item) => Number(item.dataset.placeId));
      const refreshNumbers = () => stopList.querySelectorAll('.day-detail-stop').forEach((item, index) => {
        item.querySelector('.day-stop-number').textContent = String(index + 1);
      });
      const insertBeforeAt = (y) => {
        const others = [...stopList.querySelectorAll('.day-detail-stop:not(.dragging)')];
        return others.reduce((closest, item) => {
          const box = item.getBoundingClientRect();
          const offset = y - box.top - box.height / 2;
          return offset < 0 && offset > closest.offset ? {offset, item} : closest;
        }, {offset: Number.NEGATIVE_INFINITY, item: null}).item;
      };
      const saveOrder = async () => {
        const placeIds = readOrder();
        statusNode.textContent = '방문 순서를 저장하고 있어요…';
        try {
          const response = await fetch(stopList.dataset.reorderUrl, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({place_ids: placeIds}),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || '순서를 저장하지 못했습니다.');
          if (window.updateDayMarkerOrder) window.updateDayMarkerOrder(placeIds);
          statusNode.textContent = '방문 순서를 저장했어요.';
        } catch (error) {
          previousOrder.forEach((id) => {
            const item = stopList.querySelector(`.day-detail-stop[data-place-id="${id}"]`);
            if (item) stopList.append(item);
          });
          refreshNumbers();
          if (window.updateDayMarkerOrder) window.updateDayMarkerOrder(previousOrder);
          statusNode.textContent = error.message;
        }
      };
      stopList.querySelectorAll('.day-detail-stop').forEach((item) => {
        item.addEventListener('dragstart', (event) => {
          if (event.target.closest('form, details, input, select, textarea, button')) return event.preventDefault();
          if (item.classList.contains('is-locked')) return event.preventDefault();
          previousOrder = readOrder();
          draggedStop = item;
          item.classList.add('dragging');
          event.dataTransfer.effectAllowed = 'move';
          event.dataTransfer.setData('text/plain', item.dataset.placeId);
        });
        item.addEventListener('dragend', () => {
          item.classList.remove('dragging');
          if (draggedStop) {
            previousOrder.forEach((id) => {
              const previousItem = stopList.querySelector(`.day-detail-stop[data-place-id="${id}"]`);
              if (previousItem) stopList.append(previousItem);
            });
            refreshNumbers();
            draggedStop = null;
          }
        });
      });
      stopList.addEventListener('dragover', (event) => {
        if (!draggedStop) return;
        event.preventDefault();
        const after = insertBeforeAt(event.clientY);
        if (after) stopList.insertBefore(draggedStop, after);
        else stopList.append(draggedStop);
        refreshNumbers();
      });
      stopList.addEventListener('drop', async (event) => {
        if (!draggedStop) return;
        event.preventDefault();
        draggedStop.classList.remove('dragging');
        draggedStop = null;
        await saveOrder();
      });
    }
