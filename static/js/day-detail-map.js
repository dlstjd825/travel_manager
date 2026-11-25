window.initTravelMap = async function () {
      const points = JSON.parse(document.getElementById('map-points').textContent);
      const [{Map: GoogleMap, InfoWindow}, {LatLngBounds}] = await Promise.all([
        google.maps.importLibrary('maps'), google.maps.importLibrary('core'),
      ]);
      const {AdvancedMarkerElement} = await google.maps.importLibrary('marker');
      const map = new GoogleMap(document.getElementById('trip-map'), {
        center: points.length ? {lat: points[0].latitude, lng: points[0].longitude} : {lat: 25, lng: 0},
        zoom: points.length ? 12 : 2, mapId: 'DEMO_MAP_ID', mapTypeControl: false,
        streetViewControl: false, fullscreenControl: true,
      });
      const bounds = new LatLngBounds();
      const infoWindow = new InfoWindow();
      const showPoint = (point, marker) => {
        const content = document.createElement('div');
        content.className = 'map-info-content';
        const title = document.createElement('strong');
        title.textContent = point.name;
        const address = document.createElement('p');
        address.textContent = point.kind === 'accommodation' && point.check_in_date
          ? `${point.address || ''}${point.address ? ' · ' : ''}${point.check_in_date} – ${point.check_out_date}`
          : (point.address || '');
        content.append(title, address);
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
          glyph.textContent = point.sequence_number ? String(point.sequence_number) : '•';
        }
        const marker = new AdvancedMarkerElement({
          map, position: {lat: point.latitude, lng: point.longitude}, title: point.name,
          gmpClickable: true, zIndex: point.kind === 'accommodation' ? 1000 : 100,
          anchorLeft: '-50%', anchorTop: '-50%', content: glyph,
        });
        marker.addListener('click', () => showPoint(point, marker));
        bounds.extend(marker.position);
        return {point, marker};
      });
      if (markers.length > 1) map.fitBounds(bounds, 55);
      else if (markers.length === 1) map.setZoom(14);
      window.updateDayMarkerOrder = (placeIds) => {
        placeIds.forEach((id, index) => {
          const entry = markers.find(({point}) => point.id === `place-${id}`);
          if (entry) entry.marker.content.textContent = String(index + 1);
        });
      };
    };
