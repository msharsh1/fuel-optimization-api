"""Static city index built from the database at startup — no external APIs.

The index groups FuelStation records by (city, state) and stores:
  - Approximate city centroid (median of station lat/lon, or state centroid fallback)
  - Django PKs of all stations in that city

This allows route_projection to pre-filter candidate stations by geographic
proximity without any geocoding API calls.
"""

from __future__ import annotations

import logging
import math
import threading
import statistics
from dataclasses import dataclass, field
from typing import Iterator

logger = logging.getLogger(__name__)

_MILES_PER_DEG_LAT = 69.0
_MILES_PER_DEG_LON = 53.0  # approximate for mid-US


@dataclass
class CityEntry:
    """Lightweight city record built from station data."""

    city: str
    state: str
    lat: float   # approximate centroid latitude
    lon: float   # approximate centroid longitude
    station_ids: list[int] = field(default_factory=list)


class CityIndex:
    """Thread-safe, in-memory city index populated from the DB at startup."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], CityEntry] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_from_db(self) -> None:
        """Build the index from FuelStation records. Safe to call once."""
        with self._lock:
            if self._loaded:
                return
            self._entries = _build_from_db()
            self._loaded = True
            logger.info("CityIndex loaded: %d cities from DB", len(self._entries))

    @property
    def loaded(self) -> bool:
        return self._loaded

    def nearest_city(
        self,
        lat: float,
        lon: float,
        *,
        radius_miles: float = 60.0,
    ) -> CityEntry | None:
        """Return the nearest city whose centroid is within *radius_miles*."""
        best: CityEntry | None = None
        best_dist = radius_miles

        with self._lock:
            for entry in self._entries.values():
                dist = _approx_miles(lat, lon, entry.lat, entry.lon)
                if dist < best_dist:
                    best_dist = dist
                    best = entry

        return best

    def cities_within_radius(
        self,
        lat: float,
        lon: float,
        radius_miles: float = 60.0,
    ) -> list[CityEntry]:
        """Return all cities within *radius_miles*, sorted by proximity."""
        results: list[tuple[float, CityEntry]] = []

        with self._lock:
            for entry in self._entries.values():
                dist = _approx_miles(lat, lon, entry.lat, entry.lon)
                if dist <= radius_miles:
                    results.append((dist, entry))

        results.sort(key=lambda x: x[0])
        return [e for _, e in results]

    def all_station_ids(self) -> list[int]:
        """Every station ID in the index."""
        with self._lock:
            return [sid for e in self._entries.values() for sid in e.station_ids]

    def __iter__(self) -> Iterator[CityEntry]:
        with self._lock:
            return iter(list(self._entries.values()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_city_index: CityIndex = CityIndex()


def get_city_index() -> CityIndex:
    """Return the shared singleton CityIndex."""
    return _city_index


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _build_from_db() -> dict[tuple[str, str], CityEntry]:
    """Read FuelStation table and build city → CityEntry mapping."""
    from core.models import FuelStation  # deferred to avoid circular imports

    entries: dict[tuple[str, str], CityEntry] = {}
    lat_acc: dict[tuple[str, str], list[float]] = {}
    lon_acc: dict[tuple[str, str], list[float]] = {}

    for station in FuelStation.objects.all().iterator():
        city_raw = (station.city or "").strip()
        state_raw = (station.state or "").strip().upper()
        if not city_raw or not state_raw:
            continue

        key = (city_raw.lower(), state_raw)

        # Use real coordinates when available; fall back to state centroid
        if station.latitude is not None and station.longitude is not None:
            s_lat = station.latitude
            s_lon = station.longitude
            
            # SANITY CHECK: Ignore stations clearly outside their state (e.g. Florida coords for NM state)
            state_center = _STATE_CENTROIDS.get(state_raw)
            if state_center:
                if _approx_miles(s_lat, s_lon, state_center[0], state_center[1]) > 500:
                    continue
        else:
            s_lat, s_lon = _STATE_CENTROIDS.get(state_raw, (39.5, -98.35))

        if key not in entries:
            entries[key] = CityEntry(
                city=city_raw,
                state=state_raw,
                lat=s_lat,
                lon=s_lon,
                station_ids=[station.id],
            )
            lat_acc[key] = [s_lat]
            lon_acc[key] = [s_lon]
        else:
            entries[key].station_ids.append(station.id)
            lat_acc[key].append(s_lat)
            lon_acc[key].append(s_lon)

    # Finalize centroids using median (robust against outliers)
    for key, entry in entries.items():
        if lat_acc[key]:
            entry.lat = statistics.median(lat_acc[key])
            entry.lon = statistics.median(lon_acc[key])

    return entries


def _approx_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Fast planar approximation of distance in miles."""
    dlat = (lat2 - lat1) * _MILES_PER_DEG_LAT
    dlon = (lon2 - lon1) * _MILES_PER_DEG_LON
    return math.sqrt(dlat * dlat + dlon * dlon)


# ------------------------------------------------------------------
# State centroid table (fallback for stations without coordinates)
# ------------------------------------------------------------------

_STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.806671, -86.791130), "AK": (61.370716, -152.404419),
    "AZ": (33.729759, -111.431221), "AR": (34.969704, -92.373123),
    "CA": (36.116203, -119.681564), "CO": (39.059811, -105.311104),
    "CT": (41.597782, -72.755371), "DE": (39.318523, -75.507141),
    "FL": (27.766279, -81.686783), "GA": (33.040619, -83.643074),
    "HI": (21.094318, -157.498337), "ID": (44.240459, -114.478828),
    "IL": (40.349457, -88.986137), "IN": (39.849426, -86.258278),
    "IA": (42.011539, -93.210526), "KS": (38.526600, -96.726486),
    "KY": (37.668140, -84.670067), "LA": (31.169960, -91.867805),
    "ME": (44.693947, -69.381927), "MD": (39.063946, -76.802101),
    "MA": (42.230171, -71.530106), "MI": (43.326618, -84.536095),
    "MN": (45.694454, -93.900192), "MS": (32.741646, -89.678696),
    "MO": (38.456085, -92.288368), "MT": (46.921925, -110.454353),
    "NE": (41.125370, -98.268082), "NV": (38.313515, -117.055374),
    "NH": (43.452492, -71.563896), "NJ": (40.298904, -74.521011),
    "NM": (34.840515, -106.248482), "NY": (42.165726, -74.948051),
    "NC": (35.630066, -79.806419), "ND": (47.528912, -99.784012),
    "OH": (40.388783, -82.764915), "OK": (35.565342, -96.928917),
    "OR": (44.572021, -122.070938), "PA": (40.590752, -77.209755),
    "RI": (41.680893, -71.511780), "SC": (33.856892, -80.945007),
    "SD": (44.299782, -99.438828), "TN": (35.747845, -86.692345),
    "TX": (31.054487, -97.563461), "UT": (40.150032, -111.862434),
    "HI": (21.3069, -157.8583),
    "VT": (44.045876, -72.710686), "VA": (37.769337, -78.169968),
    "WA": (47.400902, -121.490494), "WV": (38.491226, -80.954453),
    "WI": (44.268543, -89.616508), "WY": (42.755966, -107.302490),
    "DC": (38.897438, -77.026817),
}
