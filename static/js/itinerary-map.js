window.initTravelMap = async function () {
      const points = JSON.parse(document.getElementById('map-points').textContent);
      const [{Map: GoogleMap, InfoWindow}, {LatLngBounds}] = await Promise.all([
        google.maps.importLibrary('maps'), google.maps.importLibrary('core'),
      ]);
      const {AdvancedMarkerElement} = await google.maps.importLibrary('marker');
      const map = new GoogleMap(document.getElementById('trip-map'), {
        center: points.length ? {lat: points[0].latitude, lng: points[0].longitude} : {lat: 25, lng: 0},
        zoom: points.length ? 11 : 2, mapId: 'DEMO_MAP_ID', mapTypeControl: false,
        streetViewControl: false, fullscreenControl: true,
      });
      const infoWindow = new InfoWindow();
      let selectedDay = 'all';
      const showPoint = (point, marker) => {
        const content = document.createElement('div');
        content.className = 'map-info-content';
        const title = document.createElement('strong');
        title.textContent = point.name;
        const details = document.createElement('p');
        details.textContent = point.kind === 'accommodation' && point.check_in_date
          ? `${point.address || ''}${point.address ? ' · ' : ''}${point.check_in_date} – ${point.check_out_date}`
          : (point.address || '');
        content.append(title, details);
        infoWindow.setContent(content);
        infoWindow.open({map, anchor: marker});
      };
      const markers = points.map((point) => {
        const glyph = document.createElement('div');
        if (point.kind === 'accommodation') {
          glyph.className = 'map-home-glyph';
          glyph.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M3 11 12 4l9 7M5 10v10h14V10M9 20v-6h6v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        } else {
          glyph.className = `map-place-marker ${point.category === 'restaurant' ? 'restaurant-marker' : 'attraction-marker'}`;
          glyph.classList.add('day-marker');
          glyph.textContent = point.day_number ? `Day${point.day_number}` : '미정';
        }
        const marker = new AdvancedMarkerElement({
          map, position: {lat: point.latitude, lng: point.longitude}, title: point.name,
          gmpClickable: true, zIndex: point.kind === 'accommodation' ? 1000 : 100,
          anchorLeft: '-50%', anchorTop: '-50%', content: glyph,
        });
        marker.addListener('click', () => showPoint(point, marker));
        return {point, marker};
      });
      const showDay = (day) => {
        selectedDay = day;
        const bounds = new LatLngBounds();
        let visible = 0;
        markers.forEach(({point, marker}) => {
          const show = point.kind === 'accommodation'
            ? (day === 'all' || !(point.check_in_date && point.check_out_date) || point.stay_day_numbers.includes(Number(day)))
            : (day === 'all' || String(point.day_number) === day);
          marker.map = show ? map : null;
          if (point.kind !== 'accommodation') {
            const dayMarker = day === 'all';
            marker.content.classList.toggle('day-marker', dayMarker);
            marker.content.textContent = dayMarker
              ? (point.day_number ? `Day${point.day_number}` : '미정')
              : (point.sequence_number ? String(point.sequence_number) : '•');
          }
          if (show) { bounds.extend(marker.position); visible += 1; }
        });
        if (visible > 1) map.fitBounds(bounds, 55);
        else if (visible === 1) { map.panTo(bounds.getCenter()); map.setZoom(14); }
        document.querySelectorAll('.map-day-filter').forEach((button) => button.classList.toggle('active', button.dataset.day === day));
      };
      window.updateItineraryMapDays = (assignments) => {
        markers.forEach(({point}) => {
          if (point.kind === 'accommodation') return;
          const assignment = assignments[point.id.replace('place-', '')] || {};
          point.day_number = assignment.day_number || null;
          point.sequence_number = assignment.sequence_number || null;
        });
        showDay(selectedDay);
      };
      document.querySelectorAll('.map-day-filter').forEach((button) => button.addEventListener('click', () => showDay(button.dataset.day)));
      showDay('all');
    };
