from datetime import date, timedelta

from services.clustering import cluster_places
from services.distance import haversine_km

_DEFAULT_ENDPOINT = object()


def recommend_itinerary(places, accommodation, start_date, day_count, day_availability=None):
    """Assign nearby place groups to days without optimizing stop order."""
    if day_count < 1:
        return []
    start = date.fromisoformat(start_date)
    availability = day_availability or {}
    days = [
        {
            "day_number": number,
            "date": (start + timedelta(days=number - 1)).isoformat(),
            "group_numbers": [],
            "places": [],
            "route_distance_km": 0.0,
            **availability.get(number, {}),
        }
        for number in range(1, day_count + 1)
    ]
    eligible_days = [day for day in days if day.get("is_available", True)]
    if not eligible_days:
        return days

    cluster_anchor = None if isinstance(accommodation, (list, tuple)) else accommodation
    groups = cluster_places(places, cluster_anchor, max_groups=len(eligible_days))
    # Give full days the larger geographic clusters; shorter travel windows
    # receive smaller clusters where possible.
    groups = sorted(groups, key=lambda group: (-len(group["members"]), group["number"]))
    eligible_days = sorted(
        eligible_days,
        key=lambda day: (-day.get("available_hours", 12), day["day_number"]),
    )

    # Each geographic group is assigned to a travel day; when there are fewer
    # distinct locations than dates, the remaining dates stay open for flexibility.
    for index, group in enumerate(groups):
        if index < len(eligible_days):
            day = eligible_days[index]
        else:
            day = min(eligible_days, key=lambda candidate: (
                len(candidate["places"]), candidate["day_number"]
            ))
        day["group_numbers"].append(group["number"])
        day["places"].extend(group["members"])

    # Places without coordinates cannot enter KMeans. Distribute them to the
    # lightest days so that they are still present in the saved recommendation.
    group_place_ids = {place["id"] for group in groups for place in group["members"]}
    for place in places:
        if place["id"] in group_place_ids:
            continue
        day = min(eligible_days, key=lambda candidate: (
            len(candidate["places"]), candidate["day_number"]
        ))
        day["places"].append(place)

    for day in days:
        day["places"] = sorted(day["places"], key=lambda place: place["name"].casefold())
    return days


def optimize_day_order(places, accommodation, locked_place_ids=()):
    """Optimize one day while keeping locked places in their current slots."""
    locked_ids = set(locked_place_ids)
    locked_indices = [index for index, place in enumerate(places) if place["id"] in locked_ids]
    if not locked_indices:
        return _optimize_daily_order(places, accommodation)

    home = None
    if accommodation and accommodation["latitude"] is not None and accommodation["longitude"] is not None:
        home = (float(accommodation["latitude"]), float(accommodation["longitude"]))
    ordered = []
    previous_index = -1
    start_location = home
    for locked_index in locked_indices:
        locked_place = places[locked_index]
        locked_location = None
        if locked_place["latitude"] is not None and locked_place["longitude"] is not None:
            locked_location = (float(locked_place["latitude"]), float(locked_place["longitude"]))
        segment = places[previous_index + 1:locked_index]
        ordered.extend(_optimize_daily_order(
            segment, None, start_location=start_location, end_location=locked_location
        ))
        ordered.append(locked_place)
        previous_index = locked_index
        start_location = locked_location

    final_segment = places[previous_index + 1:]
    ordered.extend(_optimize_daily_order(
        final_segment, None, start_location=start_location, end_location=home
    ))
    return ordered


def _optimize_daily_order(
    places, accommodation, start_location=_DEFAULT_ENDPOINT, end_location=_DEFAULT_ENDPOINT
):
    located = [place for place in places if place["latitude"] is not None and place["longitude"] is not None]
    unlocated = [place for place in places if place["latitude"] is None or place["longitude"] is None]
    if not located:
        return sorted(unlocated, key=lambda place: place["name"].casefold())

    coordinates = [
        (float(place["latitude"]), float(place["longitude"])) for place in located
    ]
    has_home = bool(
        accommodation
        and accommodation["latitude"] is not None
        and accommodation["longitude"] is not None
    )
    home = (
        (float(accommodation["latitude"]), float(accommodation["longitude"]))
        if has_home else None
    )
    start = home if start_location is _DEFAULT_ENDPOINT else start_location
    end = home if end_location is _DEFAULT_ENDPOINT else end_location
    count = len(located)

    def distance(left, right):
        return haversine_km(*left, *right)

    if count <= 12:
        # Held–Karp dynamic programming gives the shortest route for normal
        # day-sized groups, including the trip from and back to the hotel.
        full_mask = (1 << count) - 1
        costs = {}
        parents = {}
        for index, coordinate in enumerate(coordinates):
            costs[(1 << index, index)] = distance(start, coordinate) if start else 0.0

        for mask in range(1, full_mask + 1):
            for last in range(count):
                state = (mask, last)
                current_cost = costs.get(state)
                if current_cost is None:
                    continue
                for next_index in range(count):
                    bit = 1 << next_index
                    if mask & bit:
                        continue
                    next_state = (mask | bit, next_index)
                    candidate = current_cost + distance(coordinates[last], coordinates[next_index])
                    if candidate < costs.get(next_state, float("inf")):
                        costs[next_state] = candidate
                        parents[next_state] = last

        last = min(
            range(count),
            key=lambda index: costs[(full_mask, index)]
            + (distance(coordinates[index], end) if end else 0.0),
        )
        order = []
        mask = full_mask
        while True:
            order.append(last)
            previous = parents.get((mask, last))
            mask ^= 1 << last
            if previous is None:
                break
            last = previous
        order.reverse()
    else:
        order = _two_opt_order(coordinates, start, end)

    return [located[index] for index in order] + sorted(
        unlocated, key=lambda place: place["name"].casefold()
    )


def _two_opt_order(coordinates, start, end):
    """Use nearest-neighbor seeds and 2-opt when a day has many stops."""
    count = len(coordinates)
    starts = [min(
        range(count), key=lambda index: haversine_km(*start, *coordinates[index])
    )] if start else range(count)
    best_order = None
    best_cost = float("inf")

    for start in starts:
        remaining = set(range(count))
        remaining.remove(start)
        order = [start]
        while remaining:
            current = coordinates[order[-1]]
            next_index = min(
                remaining,
                key=lambda index: haversine_km(*current, *coordinates[index]),
            )
            order.append(next_index)
            remaining.remove(next_index)

        improved = True
        while improved:
            improved = False
            for left in range(count - 1):
                for right in range(left + 1, count):
                    first = coordinates[order[left]]
                    last = coordinates[order[right]]
                    old_cost = new_cost = 0.0
                    if left == 0:
                        if start:
                            old_cost += haversine_km(*start, *first)
                            new_cost += haversine_km(*start, *last)
                    else:
                        before = coordinates[order[left - 1]]
                        old_cost += haversine_km(*before, *first)
                        new_cost += haversine_km(*before, *last)
                    if right == count - 1:
                        if end:
                            old_cost += haversine_km(*last, *end)
                            new_cost += haversine_km(*first, *end)
                    else:
                        after = coordinates[order[right + 1]]
                        old_cost += haversine_km(*last, *after)
                        new_cost += haversine_km(*first, *after)
                    if new_cost + 1e-9 < old_cost:
                        order[left:right + 1] = reversed(order[left:right + 1])
                        improved = True

        route_cost = _order_distance(order, coordinates, start, end)
        if route_cost < best_cost:
            best_order, best_cost = order, route_cost
    return best_order


def _order_distance(order, coordinates, start, end):
    if not order:
        return 0.0
    distance = 0.0
    if start:
        distance += haversine_km(*start, *coordinates[order[0]])
    for left, right in zip(order, order[1:]):
        distance += haversine_km(*coordinates[left], *coordinates[right])
    if end:
        distance += haversine_km(*coordinates[order[-1]], *end)
    return distance


def _route_distance(places, accommodation):
    located = [place for place in places if place["latitude"] is not None and place["longitude"] is not None]
    if not located:
        return 0.0
    if accommodation and accommodation["latitude"] is not None and accommodation["longitude"] is not None:
        current = (float(accommodation["latitude"]), float(accommodation["longitude"]))
    else:
        current = (float(located[0]["latitude"]), float(located[0]["longitude"]))
    distance = 0.0
    for place in located:
        next_point = (float(place["latitude"]), float(place["longitude"]))
        distance += haversine_km(*current, *next_point)
        current = next_point
    if accommodation and accommodation["latitude"] is not None and accommodation["longitude"] is not None:
        distance += haversine_km(
            *current, float(accommodation["latitude"]), float(accommodation["longitude"])
        )
    return distance
