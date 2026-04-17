import os
from functools import lru_cache

import httpx
from dotenv import load_dotenv

load_dotenv()

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

http_client: httpx.AsyncClient | None = None

_geocode_cache: dict[str, dict | None] = {}


async def geocode_address(address: str) -> dict | None:
    clean_address = (address or "").strip()
    if not clean_address:
        return None

    cache_key = clean_address.lower()
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    result = await _geocode_with_google(clean_address)
    if not result:
        result = await _geocode_with_nominatim(clean_address)

    if len(_geocode_cache) < 256:
        _geocode_cache[cache_key] = result
    return result


def _get_client() -> tuple[httpx.AsyncClient, bool]:
    if http_client is not None:
        return http_client, False
    return httpx.AsyncClient(timeout=10.0), True


async def _geocode_with_google(address: str) -> dict | None:
    if not GOOGLE_PLACES_API_KEY:
        return None

    params = {"address": address, "key": GOOGLE_PLACES_API_KEY}
    client, close_after = _get_client()
    try:
        response = await client.get(GOOGLE_GEOCODE_URL, params=params)
        if response.status_code != 200:
            return None

        payload = response.json()
        if payload.get("status") != "OK":
            return None

        results = payload.get("results") or []
        if not results:
            return None

        first = results[0]
        location = first.get("geometry", {}).get("location", {})
        lat = location.get("lat")
        lng = location.get("lng")
        if lat is None or lng is None:
            return None

        return {
            "latitude": float(lat),
            "longitude": float(lng),
            "formatted_address": first.get("formatted_address", address),
        }
    finally:
        if close_after:
            await client.aclose()


async def _geocode_with_nominatim(address: str) -> dict | None:
    params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": "CuraConnect/1.0"}

    client, close_after = _get_client()
    try:
        response = await client.get(NOMINATIM_URL, params=params, headers=headers)
        if response.status_code != 200:
            return None

        results = response.json()
        if not isinstance(results, list) or not results:
            return None

        first = results[0]
        lat = first.get("lat")
        lon = first.get("lon")
        if lat is None or lon is None:
            return None

        return {
            "latitude": float(lat),
            "longitude": float(lon),
            "formatted_address": first.get("display_name", address),
        }
    finally:
        if close_after:
            await client.aclose()
