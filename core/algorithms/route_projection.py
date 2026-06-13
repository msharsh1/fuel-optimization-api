"""Hybrid route-projection pipeline.

Since ALL stations in the database have real lat/lon coordinates, the
pipeline works as follows (all offline after the single ORS call):

  1. Decode encoded polyline → lat/lng point list.
  2. Sample route every ~SAMPLE_INTERVAL_MILES → checkpoint list.
  3. For each checkpoint: use CityIndex to find nearby cities (by state
     centroid approximation) → collect candidate station IDs.
     Fallback: if CityIndex yields no candidates (e.g. very remote route),
     use ALL stations.
  4. Fetch candidate stations from DB (bulk query).
  5. For each candidate station: compute true perpendicular distance to the
     full polyline using real lat/lon; discard if > MAX_OFF_ROUTE_MILES.
  6. Assign route_miles = projection position along polyline.
  7. Return ProjectedStation list sorted by route position.

No geocoding API is used at any point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from core.models import FuelStation
from core.services.route_service import RouteResult
from core.utils.city_index import get_city_index
from core.utils.polyline_utils import (
    cumulative_distances_miles,
    decode_polyline,
    haversine_miles,
    point_to_polyline_distance_miles,
)

logger = logging.getLogger(__name__)

# ── Tunable constants ──────────────────────────────────────────────────────────

# How far apart (miles) to place route checkpoints when sampling the polyline.
SAMPLE_INTERVAL_MILES: float = 40.0

# Radius (miles) around each checkpoint used when scanning the city index.
# Since city index uses state-centroid approx, this needs to be generous.
CITY_SEARCH_RADIUS_MILES: float = 250.0

# Maximum perpendicular distance (miles) a station may sit off the route.
MAX_OFF_ROUTE_MILES: float = getattr(settings, "MAX_OFF_ROUTE_MILES", 15.0)


# ── Data model ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectedStation:
    station_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    latitude: float
    longitude: float
    route_miles: float          # position along route polyline
    off_route_miles: float      # perpendicular distance from route


# ── Public entry-point ─────────────────────────────────────────────────────────


def project_stations_onto_route(
    route: RouteResult,
    *,
    sample_interval_miles: float = SAMPLE_INTERVAL_MILES,
    city_radius_miles: float = CITY_SEARCH_RADIUS_MILES,
    max_off_route_miles: float = MAX_OFF_ROUTE_MILES,
) -> list[ProjectedStation]:
    """Return stations projected onto the route, sorted by route position.

    Uses real station lat/lon from the database for all distance calculations.
    City index is used only as a pre-filter to reduce DB query scope.
    """
    # Step 1 – decode polyline
    polyline = decode_polyline(route.encoded_polyline)
    if len(polyline) < 2:
        logger.warning("Polyline too short to project stations onto.")
        return []

    cumulative = cumulative_distances_miles(polyline)
    total_miles = cumulative[-1] if cumulative else 0.0
    if total_miles <= 0:
        return []

    # Step 2 – sample checkpoints
    checkpoints = _sample_checkpoints(polyline, cumulative, sample_interval_miles)
    logger.debug(
        "Route sampled into %d checkpoints (%.1f mi total)", len(checkpoints), total_miles
    )

    # Step 3 – collect candidate station IDs via city index
    city_index = get_city_index()
    candidate_ids: set[int] | None = None

    if city_index.loaded:
        candidate_ids = set()
        for lat, lon in checkpoints:
            nearby_cities = city_index.cities_within_radius(lat, lon, city_radius_miles)
            for city_entry in nearby_cities:
                candidate_ids.update(city_entry.station_ids)
        logger.debug(
            "City index pre-filter: %d candidate station IDs", len(candidate_ids)
        )

    # Step 4 – bulk fetch candidates from DB
    if candidate_ids is None or len(candidate_ids) == 0:
        # Fallback: use all stations (city index not loaded or route too remote)
        logger.info(
            "City index yielded no candidates — falling back to full station scan."
        )
        stations: list[FuelStation] = list(FuelStation.objects.all())
    else:
        stations = list(FuelStation.objects.filter(id__in=candidate_ids))

    logger.debug("Fetched %d stations from DB for route filtering", len(stations))

    # Steps 5 & 6 – distance filter + route position using real coordinates
    projected: list[ProjectedStation] = []

    for station in stations:
        lat = station.latitude
        lon = station.longitude

        if lat is None or lon is None:
            logger.debug("Station %d has no coordinates — skipped", station.id)
            continue

        off_route, route_pos = point_to_polyline_distance_miles(lat, lon, polyline)

        if off_route > max_off_route_miles:
            continue
        if route_pos > total_miles + 1.0:  # allow 1mi slop at end
            continue

        projected.append(
            ProjectedStation(
                station_id=station.id,
                name=station.name,
                address=station.address,
                city=station.city,
                state=station.state,
                price=station.price,
                latitude=station.latitude,
                longitude=station.longitude,
                route_miles=round(route_pos, 2),
                off_route_miles=round(off_route, 2),
            )
        )

    projected.sort(key=lambda s: (s.route_miles, s.price))
    logger.info(
        "%d stations passed route-distance filter (max %.1f mi off route)",
        len(projected),
        max_off_route_miles,
    )
    return projected


# ── Internal helpers ───────────────────────────────────────────────────────────


def _sample_checkpoints(
    polyline: list[tuple[float, float]],
    cumulative: list[float],
    interval_miles: float,
) -> list[tuple[float, float]]:
    """Return evenly spaced (lat, lon) samples from the polyline."""
    if not polyline or interval_miles <= 0:
        return polyline[:] if polyline else []

    total = cumulative[-1]
    samples: list[tuple[float, float]] = [polyline[0]]  # always include start
    next_target = interval_miles

    for i in range(1, len(polyline)):
        if cumulative[i] >= next_target:
            samples.append(polyline[i])
            next_target += interval_miles
            if next_target > total:
                break

    if polyline[-1] not in samples:
        samples.append(polyline[-1])  # always include end

    return samples
