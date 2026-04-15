import os
import httpx
from dotenv import load_dotenv

load_dotenv()

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
PLACES_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"


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
        return _mock_doctors(latitude, longitude)

    keyword = specialty if specialty else "doctor clinic"
    params = {
        "location": f"{latitude},{longitude}",
        "radius": radius_m,
        "type": "doctor",
        "keyword": keyword,
        "key": GOOGLE_PLACES_API_KEY,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(PLACES_NEARBY_URL, params=params)
        if response.status_code != 200:
            return _mock_doctors(latitude, longitude)

        data = response.json()
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

        return doctors


def _mock_doctors(lat: float, lng: float) -> list[dict]:
    """Fallback mock data when no API key is configured."""
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
