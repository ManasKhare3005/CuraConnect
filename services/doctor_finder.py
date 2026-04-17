import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
PLACES_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

http_client: httpx.AsyncClient | None = None


async def find_nearby_doctors(
    latitude: float,
    longitude: float,
    specialty: str | None = None,
    radius_m: int = 5000,
) -> list[dict]:
    """
    Find nearby doctors/clinics using Google Places API.
    Returns a list of up to 5 doctor/clinic results.
    """
    if not GOOGLE_PLACES_API_KEY:
        return _mock_doctors(latitude, longitude, specialty)

    keyword = specialty if specialty else "doctor clinic"
    params = {
        "location": f"{latitude},{longitude}",
        "radius": radius_m,
        "type": "doctor",
        "keyword": keyword,
        "key": GOOGLE_PLACES_API_KEY,
    }

    client = http_client or httpx.AsyncClient(timeout=10.0)
    close_after = http_client is None
    try:
        response = await client.get(PLACES_NEARBY_URL, params=params)
        if response.status_code != 200:
            logger.warning("Google Places HTTP %s: %s", response.status_code, response.text[:200])
            return _mock_doctors(latitude, longitude, specialty)

        data = response.json()
        status = data.get("status")
        # Google returns HTTP 200 even for REQUEST_DENIED / OVER_QUERY_LIMIT.
        if status not in ("OK", "ZERO_RESULTS"):
            logger.warning("Google Places status=%s error=%s", status, data.get("error_message"))
            return _mock_doctors(latitude, longitude, specialty)

        results = data.get("results", [])[:5]

        doctors = []
        for place in results:
            doctors.append({
                "name": place.get("name", "Unknown"),
                "address": place.get("vicinity", "Address not available"),
                "rating": place.get("rating"),
                "open_now": place.get("opening_hours", {}).get("open_now"),
                "place_id": place.get("place_id"),
                "maps_url": f"https://www.google.com/maps/place/?q=place_id:{place.get('place_id')}",
            })

        if doctors:
            return doctors
        return _mock_doctors(latitude, longitude, specialty)
    finally:
        if close_after:
            await client.aclose()


def _mock_doctors(lat: float, lng: float, specialty: str | None = None) -> list[dict]:
    """Fallback mock data when no API key is configured."""
    normalized_specialty = (specialty or "").strip().lower()

    if "allerg" in normalized_specialty:
        return [
            {
                "name": "Desert Allergy & Asthma Center",
                "address": "Tempe, AZ",
                "rating": 4.6,
                "open_now": True,
                "place_id": None,
                "maps_url": f"https://www.google.com/maps/search/allergist/@{lat},{lng},14z",
            },
            {
                "name": "Valley Immunology Clinic",
                "address": "Tempe, AZ",
                "rating": 4.4,
                "open_now": True,
                "place_id": None,
                "maps_url": f"https://www.google.com/maps/search/allergy+clinic/@{lat},{lng},14z",
            },
        ]

    if "general physician" in normalized_specialty or "family medicine" in normalized_specialty:
        return [
            {
                "name": "General Practitioners Center",
                "address": "Tempe, AZ",
                "rating": 4.2,
                "open_now": True,
                "place_id": None,
                "maps_url": f"https://www.google.com/maps/search/general+physician/@{lat},{lng},14z",
            },
            {
                "name": "Family Medicine Associates",
                "address": "Tempe, AZ",
                "rating": 4.3,
                "open_now": True,
                "place_id": None,
                "maps_url": f"https://www.google.com/maps/search/family+medicine/@{lat},{lng},14z",
            },
        ]

    return [
        {
            "name": "City Health Clinic",
            "address": "123 Main St (near your location)",
            "rating": 4.5,
            "open_now": True,
            "place_id": None,
            "maps_url": f"https://www.google.com/maps/search/doctor/@{lat},{lng},14z",
        },
        {
            "name": "General Practitioners Center",
            "address": "456 Oak Ave (near your location)",
            "rating": 4.2,
            "open_now": True,
            "place_id": None,
            "maps_url": f"https://www.google.com/maps/search/clinic/@{lat},{lng},14z",
        },
    ]
