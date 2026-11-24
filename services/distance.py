from math import asin, cos, radians, sin, sqrt


EARTH_RADIUS_KM = 6371.0088


def haversine_km(latitude_a, longitude_a, latitude_b, longitude_b):
    """Return great-circle distance between two WGS84 coordinates in kilometers."""
    lat_a, lon_a, lat_b, lon_b = map(
        radians, (latitude_a, longitude_a, latitude_b, longitude_b)
    )
    delta_lat = lat_b - lat_a
    delta_lon = lon_b - lon_a
    value = (
        sin(delta_lat / 2) ** 2
        + cos(lat_a) * cos(lat_b) * sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * asin(sqrt(min(1.0, value)))
