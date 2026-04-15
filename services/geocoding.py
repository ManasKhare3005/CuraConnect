import os

import httpx
from dotenv import load_dotenv

load_dotenv()

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


async def geocode_address(address: str) -> dict | None:
    """
    Resolve an address to latitude/longitude.
    Uses Google Geocoding API when key is available, then falls back to Nominatim.
    """
    clean_address = (address or "").strip()
    if not clean_address:
        return None

    google_result = await _geocode_with_google(clean_address)
    if google_result:
        return google_result

    return await _geocode_with_nominatim(clean_address)


async def _geocode_with_google(address: str) -> dict | None:
    if not GOOGLE_PLACES_API_KEY:
        return None

    params = {"address": address, "key": GOOGLE_PLACES_API_KEY}
    async with httpx.AsyncClient(timeout=10.0) as client:
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


async def _geocode_with_nominatim(address: str) -> dict | None:
    params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": "CuraConnect/1.0"}

    async with httpx.AsyncClient(timeout=10.0) as client:
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
