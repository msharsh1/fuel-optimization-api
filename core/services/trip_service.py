"""Orchestrate the full hybrid fuel-optimization pipeline.

Pipeline (single ORS call + all-offline processing):
  1. get_route()                   → ORS Directions API (exactly 1 call)
  2. project_stations_onto_route() → polyline sampling + city index + distance filter
  3. optimize_fuel_stops()         → greedy look-ahead optimizer
  4. Return structured dict matching the canonical API response format.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.algorithms.greedy_optimizer import FuelPlan, FuelStop, optimize_fuel_stops
from core.algorithms.route_projection import ProjectedStation, project_stations_onto_route
from core.services.route_service import RouteResult, RouteService

logger = logging.getLogger(__name__)


# ── Data models ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TripPlan:
    route: RouteResult
    projected_stations: tuple[ProjectedStation, ...]
    fuel_plan: FuelPlan


# ── City-name resolver (offline, DB-only) ──────────────────────────────────────


def resolve_location(location: str) -> str:
    """Attempt to resolve a city name to 'lat,lon' using the DB city index.

    If the input is already a valid 'lat,lon' string, return it unchanged.
    If it looks like 'City, ST' or 'City, State', look it up in the CityIndex
    (which holds real station centroids) and return the centroid coordinates.

    Raises ValueError if the input cannot be resolved without an external API.
    No geocoding API is called at any point.
    """
    import re
    from core.utils.city_index import get_city_index

    # Already a coordinate?
    coord_pat = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*$")
    if coord_pat.match(location):
        return location.strip()

    # Try to parse as "City, ST" or "City, State"
    # Normalize: strip, collapse whitespace
    text = " ".join(location.strip().split())

    # Pattern: anything before a comma, then whitespace + 2-char state abbr OR state name
    city_state_pat = re.compile(
        r"^(.+?)\s*,\s*([A-Za-z]{2}|[A-Za-z ]{4,})$"
    )
    m = city_state_pat.match(text)
    if not m:
        raise ValueError(
            f"Cannot resolve {location!r}: not a 'lat,lon' coordinate and not "
            "recognized as 'City, ST' format. No geocoding API is available."
        )

    city_part = m.group(1).strip().lower()
    state_part = m.group(2).strip().upper()

    # Normalize full state names to 2-letter codes
    state_part = _STATE_NAME_TO_CODE.get(state_part, state_part[:2] if len(state_part) > 2 else state_part)

    idx = get_city_index()
    if not idx.loaded:
        raise ValueError("City index not loaded — cannot resolve city names offline.")

    # Direct key lookup
    key = (city_part, state_part)
    for entry in idx:
        if entry.city.lower() == city_part and entry.state.upper() == state_part:
            lat, lon = entry.lat, entry.lon
            logger.info(
                "Resolved %r -> (%.5f, %.5f) via CityIndex", location, lat, lon
            )
            return f"{lat},{lon}"

    raise ValueError(
        f"City {location!r} not found in the fuel station dataset. "
        "Only cities present in the dataset can be used without a geocoding API."
    )


# ── Service ────────────────────────────────────────────────────────────────────


class TripService:
    """High-level trip planning — no geocoding API, exactly 1 ORS call."""

    def __init__(self, route_service: RouteService | None = None) -> None:
        self.route_service = route_service or RouteService()

    def plan_trip(self, start: str, end: str) -> TripPlan:
        """Plan a trip from *start* to *end*.

        Both *start* and *end* can be:
          - 'lat,lon' coordinate strings (preferred)
          - 'City, ST' strings (resolved offline via CityIndex)

        Returns a TripPlan with route geometry, filtered projected stations,
        and the greedy fuel plan.
        """
        # Resolve city names → lat,lon if needed (offline, DB only)
        start_coord = resolve_location(start)
        end_coord = resolve_location(end)

        # Step 1 – one ORS Directions call (or cache hit = 0 API calls)
        route = self.route_service.get_route(start_coord, end_coord)
        logger.info(
            "Route: %.1f mi, %.0f sec (ORS calls used: %d)",
            route.distance_miles,
            route.duration_seconds,
            route.api_calls_used,
        )

        # Steps 2–6 – fully offline projection pipeline
        projected = project_stations_onto_route(route)
        logger.info("%d stations projected onto route", len(projected))

        # Step 7 – greedy optimizer
        fuel_plan = optimize_fuel_stops(projected, route.distance_miles)
        logger.info(
            "Fuel plan: %d stops, total cost $%.2f",
            len(fuel_plan.stops),
            fuel_plan.total_fuel_cost_usd,
        )

        return TripPlan(
            route=route,
            projected_stations=tuple(projected),
            fuel_plan=fuel_plan,
        )

    def plan_trip_as_dict(self, start: str, end: str) -> dict:
        """Return the trip plan as the canonical API response dict."""
        plan = self.plan_trip(start, end)
        return _serialize_trip_plan(plan)


# ── Serialization ──────────────────────────────────────────────────────────────


def _serialize_trip_plan(plan: TripPlan) -> dict:
    """Convert a TripPlan into the canonical JSON response structure.

    Output format (exactly as specified):
    {
        "route": { "distance": float, "duration": float, "polyline": str },
        "fuel_stops": [ { "name", "city", "price", "gallons", "cost" } ],
        "total_cost": float
    }
    """
    return {
        "route": {
            "distance": round(plan.route.distance_miles, 2),
            "duration": round(plan.route.duration_seconds, 0),
            "polyline": plan.route.encoded_polyline,
        },
        "fuel_stops": [_serialize_stop(stop) for stop in plan.fuel_plan.stops],
        "total_cost": plan.fuel_plan.total_fuel_cost_usd,
    }


def _serialize_stop(stop: FuelStop) -> dict:
    return {
        "name": stop.name,
        "city": stop.city,
        "latitude": stop.latitude,
        "longitude": stop.longitude,
        "price": stop.price,
        "gallons": stop.gallons,
        "cost": stop.fuel_cost_usd,
    }


# ── State name → abbreviation table ───────────────────────────────────────────

_STATE_NAME_TO_CODE: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN",
    "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC",
}
