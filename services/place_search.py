import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PlaceSearchError(Exception):
    """Safe errors suitable for display in the local planner."""


def search_places(api_key, text_query, city, limit=6):
    if not api_key:
        raise PlaceSearchError(
            "Google Places API 키가 없습니다. GOOGLE_MAPS_API_KEY 환경변수를 설정해 주세요."
        )
    text_query = text_query.strip()
    if not text_query:
        return []

    body = json.dumps({
        "textQuery": f"{text_query} {city}".strip(),
        "pageSize": limit,
        "languageCode": "ko",
    }).encode("utf-8")
    request = Request(
        "https://places.googleapis.com/v1/places:searchText",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": (
                "places.id,places.displayName,places.formattedAddress,places.location"
            ),
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in (400, 401, 403):
            raise PlaceSearchError(
                "Google Places 요청이 거부됐습니다. Places API 활성화, 결제 설정, API 키 제한을 확인해 주세요."
            ) from error
        raise PlaceSearchError("Google Places 검색에 실패했습니다. 잠시 후 다시 시도해 주세요.") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise PlaceSearchError(
            "Google Places에 연결하지 못했습니다. 네트워크 상태를 확인해 주세요."
        ) from error

    results = []
    for place in data.get("places", []):
        location = place.get("location") or {}
        label = (place.get("displayName") or {}).get("text", "").strip()
        if not label:
            continue
        results.append({
            "provider_place_id": place.get("id", ""),
            "label": label,
            "formatted_address": place.get("formattedAddress", ""),
            "latitude": location.get("latitude", ""),
            "longitude": location.get("longitude", ""),
        })
    return results
