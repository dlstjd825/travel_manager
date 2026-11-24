from math import ceil

import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from threadpoolctl import threadpool_limits

from services.distance import haversine_km


def cluster_places(places, accommodation=None, max_groups=None):
    """Choose a natural KMeans group count from the coordinate distribution."""
    located = [
        place for place in places
        if place["latitude"] is not None and place["longitude"] is not None
    ]
    if not located:
        return []

    coordinates = np.asarray(
        [[float(place["latitude"]), float(place["longitude"])] for place in located],
        dtype=float,
    )
    mean_latitude = np.radians(float(np.mean(coordinates[:, 0])))
    projected = np.column_stack((coordinates[:, 1] * np.cos(mean_latitude), coordinates[:, 0]))
    distinct_count = len(np.unique(coordinates, axis=0))
    cluster_count = _choose_cluster_count(projected, distinct_count, max_groups)
    if cluster_count == 1:
        labels = np.zeros(len(located), dtype=int)
    else:
        with threadpool_limits(limits=1):
            labels = KMeans(
                n_clusters=cluster_count,
                random_state=42,
                n_init=10,
            ).fit_predict(projected)

    home = None
    if accommodation and accommodation["latitude"] is not None and accommodation["longitude"] is not None:
        home = (float(accommodation["latitude"]), float(accommodation["longitude"]))

    clusters = []
    for label in sorted(set(labels.tolist())):
        members = [place for place, member_label in zip(located, labels) if member_label == label]
        center_lat = sum(float(place["latitude"]) for place in members) / len(members)
        center_lng = sum(float(place["longitude"]) for place in members) / len(members)
        hotel_distance = (
            haversine_km(home[0], home[1], center_lat, center_lng) if home else None
        )
        clusters.append({
            "members": members,
            "latitude": center_lat,
            "longitude": center_lng,
            "accommodation_distance_km": hotel_distance,
        })

    clusters.sort(key=lambda cluster: (
        cluster["accommodation_distance_km"]
        if cluster["accommodation_distance_km"] is not None
        else cluster["latitude"],
        cluster["longitude"],
    ))
    for number, cluster in enumerate(clusters, 1):
        cluster["number"] = number
    return clusters


def _choose_cluster_count(coordinates, distinct_count, max_groups=None):
    place_count = len(coordinates)
    max_candidate = min(8, place_count - 1, distinct_count - 1)
    if max_groups is not None:
        max_candidate = min(max_candidate, max(1, int(max_groups)))
    if max_candidate < 2:
        return 1

    best_count = 1
    best_score = 0.2
    for count in range(2, max_candidate + 1):
        with threadpool_limits(limits=1):
            model = KMeans(n_clusters=count, random_state=42, n_init=10)
            labels = model.fit_predict(coordinates)
        if len(set(labels.tolist())) < 2:
            continue
        score = silhouette_score(coordinates, labels)
        # Require visible separation and prefer fewer groups on near ties.
        if score > best_score + 0.02:
            best_score = score
            best_count = count
    # Avoid assigning an oversized group to one date while other dates are empty.
    # Keep extra days open on long trips by limiting the target to roughly one
    # group per three saved places, and only split up to the trip's day count.
    effective_group_limit = min(max_candidate, max(1, ceil(place_count / 3)))
    capacity = ceil(place_count / effective_group_limit) * 2
    selected_count = best_count
    while selected_count < effective_group_limit:
        with threadpool_limits(limits=1):
            labels = KMeans(
                n_clusters=selected_count,
                random_state=42,
                n_init=10,
            ).fit_predict(coordinates)
        if max(np.bincount(labels)) <= capacity:
            break
        selected_count += 1
    return selected_count
