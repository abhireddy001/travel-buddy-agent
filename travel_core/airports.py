"""
Reference set of major world airports used by the simulated flight provider.

Each entry: IATA code -> (name, city, country, region, latitude, longitude, IANA timezone).
Coordinates are approximate (good enough for great-circle distances).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Airport:
    code: str
    name: str
    city: str
    country: str
    region: str
    lat: float
    lon: float
    tz: str

    def label(self) -> str:
        return f"{self.city} ({self.code})"


_RAW = [
    # North America
    ("JFK", "John F. Kennedy International", "New York", "United States", "North America", 40.64, -73.78, "America/New_York"),
    ("EWR", "Newark Liberty International", "Newark", "United States", "North America", 40.69, -74.17, "America/New_York"),
    ("BOS", "Logan International", "Boston", "United States", "North America", 42.36, -71.01, "America/New_York"),
    ("IAD", "Washington Dulles International", "Washington", "United States", "North America", 38.95, -77.46, "America/New_York"),
    ("ATL", "Hartsfield-Jackson Atlanta International", "Atlanta", "United States", "North America", 33.64, -84.43, "America/New_York"),
    ("MIA", "Miami International", "Miami", "United States", "North America", 25.79, -80.29, "America/New_York"),
    ("ORD", "O'Hare International", "Chicago", "United States", "North America", 41.97, -87.91, "America/Chicago"),
    ("DFW", "Dallas/Fort Worth International", "Dallas", "United States", "North America", 32.90, -97.04, "America/Chicago"),
    ("DEN", "Denver International", "Denver", "United States", "North America", 39.86, -104.67, "America/Denver"),
    ("LAX", "Los Angeles International", "Los Angeles", "United States", "North America", 33.94, -118.41, "America/Los_Angeles"),
    ("SFO", "San Francisco International", "San Francisco", "United States", "North America", 37.62, -122.38, "America/Los_Angeles"),
    ("SEA", "Seattle-Tacoma International", "Seattle", "United States", "North America", 47.45, -122.31, "America/Los_Angeles"),
    ("LAS", "Harry Reid International", "Las Vegas", "United States", "North America", 36.08, -115.15, "America/Los_Angeles"),
    ("HNL", "Daniel K. Inouye International", "Honolulu", "United States", "North America", 21.32, -157.92, "Pacific/Honolulu"),
    ("YYZ", "Toronto Pearson International", "Toronto", "Canada", "North America", 43.68, -79.63, "America/Toronto"),
    ("YVR", "Vancouver International", "Vancouver", "Canada", "North America", 49.19, -123.18, "America/Vancouver"),
    ("YUL", "Montréal-Trudeau International", "Montreal", "Canada", "North America", 45.47, -73.74, "America/Toronto"),
    ("MEX", "Mexico City International", "Mexico City", "Mexico", "North America", 19.44, -99.07, "America/Mexico_City"),
    ("CUN", "Cancún International", "Cancun", "Mexico", "North America", 21.04, -86.88, "America/Cancun"),
    # South America
    ("GRU", "São Paulo/Guarulhos International", "Sao Paulo", "Brazil", "South America", -23.43, -46.47, "America/Sao_Paulo"),
    ("GIG", "Rio de Janeiro/Galeão International", "Rio de Janeiro", "Brazil", "South America", -22.81, -43.25, "America/Sao_Paulo"),
    ("EZE", "Ministro Pistarini International", "Buenos Aires", "Argentina", "South America", -34.82, -58.54, "America/Argentina/Buenos_Aires"),
    ("SCL", "Arturo Merino Benítez International", "Santiago", "Chile", "South America", -33.39, -70.79, "America/Santiago"),
    ("BOG", "El Dorado International", "Bogota", "Colombia", "South America", 4.70, -74.15, "America/Bogota"),
    ("LIM", "Jorge Chávez International", "Lima", "Peru", "South America", -12.02, -77.11, "America/Lima"),
    # Europe
    ("LHR", "Heathrow", "London", "United Kingdom", "Europe", 51.47, -0.45, "Europe/London"),
    ("LGW", "Gatwick", "London", "United Kingdom", "Europe", 51.15, -0.19, "Europe/London"),
    ("DUB", "Dublin", "Dublin", "Ireland", "Europe", 53.42, -6.27, "Europe/Dublin"),
    ("CDG", "Charles de Gaulle", "Paris", "France", "Europe", 49.01, 2.55, "Europe/Paris"),
    ("AMS", "Amsterdam Schiphol", "Amsterdam", "Netherlands", "Europe", 52.31, 4.76, "Europe/Amsterdam"),
    ("FRA", "Frankfurt", "Frankfurt", "Germany", "Europe", 50.04, 8.56, "Europe/Berlin"),
    ("MUC", "Munich", "Munich", "Germany", "Europe", 48.35, 11.79, "Europe/Berlin"),
    ("ZRH", "Zurich", "Zurich", "Switzerland", "Europe", 47.46, 8.55, "Europe/Zurich"),
    ("MAD", "Adolfo Suárez Madrid-Barajas", "Madrid", "Spain", "Europe", 40.49, -3.57, "Europe/Madrid"),
    ("BCN", "Barcelona-El Prat", "Barcelona", "Spain", "Europe", 41.30, 2.08, "Europe/Madrid"),
    ("LIS", "Humberto Delgado", "Lisbon", "Portugal", "Europe", 38.77, -9.13, "Europe/Lisbon"),
    ("FCO", "Leonardo da Vinci-Fiumicino", "Rome", "Italy", "Europe", 41.80, 12.25, "Europe/Rome"),
    ("MXP", "Milan Malpensa", "Milan", "Italy", "Europe", 45.63, 8.72, "Europe/Rome"),
    ("VIE", "Vienna International", "Vienna", "Austria", "Europe", 48.11, 16.57, "Europe/Vienna"),
    ("CPH", "Copenhagen", "Copenhagen", "Denmark", "Europe", 55.62, 12.65, "Europe/Copenhagen"),
    ("ARN", "Stockholm Arlanda", "Stockholm", "Sweden", "Europe", 59.65, 17.92, "Europe/Stockholm"),
    ("ATH", "Athens International", "Athens", "Greece", "Europe", 37.94, 23.94, "Europe/Athens"),
    ("IST", "Istanbul", "Istanbul", "Turkey", "Europe", 41.26, 28.74, "Europe/Istanbul"),
    # Middle East
    ("DXB", "Dubai International", "Dubai", "United Arab Emirates", "Middle East", 25.25, 55.36, "Asia/Dubai"),
    ("AUH", "Zayed International", "Abu Dhabi", "United Arab Emirates", "Middle East", 24.43, 54.65, "Asia/Dubai"),
    ("DOH", "Hamad International", "Doha", "Qatar", "Middle East", 25.27, 51.61, "Asia/Qatar"),
    ("RUH", "King Khalid International", "Riyadh", "Saudi Arabia", "Middle East", 24.96, 46.70, "Asia/Riyadh"),
    ("TLV", "Ben Gurion", "Tel Aviv", "Israel", "Middle East", 32.01, 34.89, "Asia/Jerusalem"),
    # Africa
    ("CAI", "Cairo International", "Cairo", "Egypt", "Africa", 30.12, 31.41, "Africa/Cairo"),
    ("JNB", "O. R. Tambo International", "Johannesburg", "South Africa", "Africa", -26.14, 28.24, "Africa/Johannesburg"),
    ("CPT", "Cape Town International", "Cape Town", "South Africa", "Africa", -33.97, 18.60, "Africa/Johannesburg"),
    ("NBO", "Jomo Kenyatta International", "Nairobi", "Kenya", "Africa", -1.32, 36.93, "Africa/Nairobi"),
    ("ADD", "Addis Ababa Bole International", "Addis Ababa", "Ethiopia", "Africa", 8.98, 38.80, "Africa/Addis_Ababa"),
    ("LOS", "Murtala Muhammed International", "Lagos", "Nigeria", "Africa", 6.58, 3.32, "Africa/Lagos"),
    ("CMN", "Mohammed V International", "Casablanca", "Morocco", "Africa", 33.37, -7.59, "Africa/Casablanca"),
    # Asia
    ("DEL", "Indira Gandhi International", "Delhi", "India", "Asia", 28.56, 77.10, "Asia/Kolkata"),
    ("BOM", "Chhatrapati Shivaji Maharaj International", "Mumbai", "India", "Asia", 19.09, 72.87, "Asia/Kolkata"),
    ("BLR", "Kempegowda International", "Bengaluru", "India", "Asia", 13.20, 77.71, "Asia/Kolkata"),
    ("SIN", "Singapore Changi", "Singapore", "Singapore", "Asia", 1.36, 103.99, "Asia/Singapore"),
    ("KUL", "Kuala Lumpur International", "Kuala Lumpur", "Malaysia", "Asia", 2.75, 101.71, "Asia/Kuala_Lumpur"),
    ("BKK", "Suvarnabhumi", "Bangkok", "Thailand", "Asia", 13.69, 100.75, "Asia/Bangkok"),
    ("CGK", "Soekarno-Hatta International", "Jakarta", "Indonesia", "Asia", -6.13, 106.66, "Asia/Jakarta"),
    ("MNL", "Ninoy Aquino International", "Manila", "Philippines", "Asia", 14.51, 121.02, "Asia/Manila"),
    ("HKG", "Hong Kong International", "Hong Kong", "Hong Kong SAR", "Asia", 22.31, 113.92, "Asia/Hong_Kong"),
    ("PEK", "Beijing Capital International", "Beijing", "China", "Asia", 40.08, 116.58, "Asia/Shanghai"),
    ("PVG", "Shanghai Pudong International", "Shanghai", "China", "Asia", 31.14, 121.81, "Asia/Shanghai"),
    ("ICN", "Incheon International", "Seoul", "South Korea", "Asia", 37.46, 126.44, "Asia/Seoul"),
    ("NRT", "Narita International", "Tokyo", "Japan", "Asia", 35.77, 140.39, "Asia/Tokyo"),
    ("HND", "Haneda", "Tokyo", "Japan", "Asia", 35.55, 139.78, "Asia/Tokyo"),
    ("KIX", "Kansai International", "Osaka", "Japan", "Asia", 34.43, 135.24, "Asia/Tokyo"),
    ("TPE", "Taiwan Taoyuan International", "Taipei", "Taiwan", "Asia", 25.08, 121.23, "Asia/Taipei"),
    # ASEAN leisure destinations
    ("DPS", "Ngurah Rai International", "Bali", "Indonesia", "Asia", -8.75, 115.17, "Asia/Makassar"),
    ("HKT", "Phuket International", "Phuket", "Thailand", "Asia", 8.11, 98.32, "Asia/Bangkok"),
    ("CNX", "Chiang Mai International", "Chiang Mai", "Thailand", "Asia", 18.77, 98.96, "Asia/Bangkok"),
    ("DMK", "Don Mueang International", "Bangkok", "Thailand", "Asia", 13.91, 100.61, "Asia/Bangkok"),
    ("SGN", "Tan Son Nhat International", "Ho Chi Minh City", "Vietnam", "Asia", 10.82, 106.66, "Asia/Ho_Chi_Minh"),
    ("HAN", "Noi Bai International", "Hanoi", "Vietnam", "Asia", 21.22, 105.81, "Asia/Ho_Chi_Minh"),
    ("DAD", "Da Nang International", "Da Nang", "Vietnam", "Asia", 16.04, 108.20, "Asia/Ho_Chi_Minh"),
    ("PQC", "Phu Quoc International", "Phu Quoc", "Vietnam", "Asia", 10.17, 103.99, "Asia/Ho_Chi_Minh"),
    ("SAI", "Siem Reap-Angkor International", "Siem Reap", "Cambodia", "Asia", 13.37, 104.22, "Asia/Phnom_Penh"),
    ("VTE", "Wattay International", "Vientiane", "Laos", "Asia", 17.99, 102.56, "Asia/Vientiane"),
    ("LPQ", "Luang Prabang International", "Luang Prabang", "Laos", "Asia", 19.90, 102.16, "Asia/Vientiane"),
    ("RGN", "Yangon International", "Yangon", "Myanmar", "Asia", 16.91, 96.13, "Asia/Yangon"),
    ("BWN", "Brunei International", "Bandar Seri Begawan", "Brunei", "Asia", 4.94, 114.93, "Asia/Brunei"),
    ("CEB", "Mactan-Cebu International", "Cebu", "Philippines", "Asia", 10.31, 123.98, "Asia/Manila"),
    ("MPH", "Godofredo P. Ramos (Caticlan)", "Boracay", "Philippines", "Asia", 11.92, 121.95, "Asia/Manila"),
    ("PEN", "Penang International", "Penang", "Malaysia", "Asia", 5.30, 100.28, "Asia/Kuala_Lumpur"),
    ("LGK", "Langkawi International", "Langkawi", "Malaysia", "Asia", 6.33, 99.73, "Asia/Kuala_Lumpur"),
    ("BKI", "Kota Kinabalu International", "Kota Kinabalu", "Malaysia", "Asia", 5.94, 116.05, "Asia/Kuching"),
    # Oceania
    ("SYD", "Sydney Kingsford Smith", "Sydney", "Australia", "Oceania", -33.95, 151.18, "Australia/Sydney"),
    ("MEL", "Melbourne", "Melbourne", "Australia", "Oceania", -37.67, 144.84, "Australia/Melbourne"),
    ("BNE", "Brisbane", "Brisbane", "Australia", "Oceania", -27.38, 153.12, "Australia/Brisbane"),
    ("PER", "Perth", "Perth", "Australia", "Oceania", -31.94, 115.97, "Australia/Perth"),
    ("AKL", "Auckland", "Auckland", "New Zealand", "Oceania", -37.01, 174.79, "Pacific/Auckland"),
]

AIRPORTS: dict[str, Airport] = {row[0]: Airport(*row) for row in _RAW}

# Major connecting hubs used to build one-stop itineraries.
HUBS = [
    "DXB", "DOH", "IST", "FRA", "LHR", "AMS", "CDG", "SIN", "HKG", "ICN",
    "NRT", "ORD", "ATL", "DFW", "LAX", "JFK", "GRU", "BOG", "JNB", "ADD", "SYD", "DEL",
]


class UnknownAirportError(ValueError):
    """Raised when an airport code or city cannot be resolved."""


def search_airports(query: str, limit: int = 10) -> list[Airport]:
    """Find airports whose code, city, name or country matches the query (case-insensitive)."""
    q = query.strip().lower()
    if not q:
        return []
    exact = [a for a in AIRPORTS.values() if a.code.lower() == q]
    partial = [
        a for a in AIRPORTS.values()
        if a not in exact and (
            q in a.city.lower() or q in a.name.lower() or q in a.country.lower()
        )
    ]
    return (exact + partial)[:limit]


def resolve_airport(code_or_city: str) -> Airport:
    """Resolve an IATA code (preferred) or an unambiguous city name to an Airport."""
    key = code_or_city.strip().upper()
    if key in AIRPORTS:
        return AIRPORTS[key]
    matches = [a for a in AIRPORTS.values() if a.city.lower() == code_or_city.strip().lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        codes = ", ".join(a.code for a in matches)
        raise UnknownAirportError(
            f"'{code_or_city}' has several airports ({codes}); please pick an IATA code."
        )
    raise UnknownAirportError(
        f"Unknown airport '{code_or_city}'. Use search_airports to find a supported IATA code."
    )


def distance_km(a: Airport, b: Airport) -> float:
    """Great-circle distance between two airports in kilometres (haversine)."""
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
