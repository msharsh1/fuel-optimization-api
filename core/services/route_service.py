"""OpenRouteService Directions API integration.

STRICT CONSTRAINT: ORS is called EXACTLY ONCE per request (Directions only).
No geocoding endpoint is used anywhere in this service.  Callers must supply
coordinates in "lat,lon" format.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Matches "lat,lon" or "lat, lon" with optional whitespace
COORDINATE_PATTERN = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)


class RouteServiceError(Exception):
    """Base error for route retrieval failures."""


class InvalidCoordinateError(RouteServiceError):
    """Raised when a location string is not a valid 'lat,lon' coordinate."""


class ORSCallBudgetExceeded(RouteServiceError):
    """Raised when the per-request ORS call limit would be exceeded."""


@dataclass(frozen=True)
class RouteStep:
    name: str
    distance_miles: float
    cumulative_miles: float


@dataclass(frozen=True)
class RouteResult:
    distance_miles: float
    duration_seconds: float
    encoded_polyline: str
    steps: tuple[RouteStep, ...]
    api_calls_used: int


class ORSCallBudget:
    """Hard cap: at most 1 HTTP call to ORS per request (Directions only)."""

    MAX = 1  # exactly one call allowed

    def __init__(self) -> None:
        self.used = 0

    def consume(self) -> None:
        if self.used >= self.MAX:
            raise ORSCallBudgetExceeded(
                f"ORS call budget exhausted (max {self.MAX} call per request). "
                "Only the Directions API may be called, and only once."
            )
        self.used += 1


class RouteService:
    """Fetch driving routes from ORS with aggressive response caching.

    Accepts only pre-resolved 'lat,lon' coordinate strings.
    NO geocoding is performed inside this class.
    """

    DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions/driving-car"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.api_key = settings.ORS_API_KEY
        self.timeout = settings.ORS_REQUEST_TIMEOUT
        self.max_retries = settings.ORS_MAX_RETRIES
        self.cache_ttl = settings.ROUTE_CACHE_TTL

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_route(self, start: str, end: str) -> RouteResult:
        """Return a RouteResult for two 'lat,lon' strings.

        The result is cached keyed on the (start, end) pair; a cache hit
        consumes zero ORS API calls.

        Raises
        ------
        InvalidCoordinateError
            If start or end is not a valid 'lat,lon' string.
        ORSCallBudgetExceeded
            If a second ORS call would be made within this request.
        RouteServiceError
            For network failures or unexpected ORS responses.
        """
        start_key = _normalize_coord_key(start)
        end_key = _normalize_coord_key(end)
        cache_key = _route_cache_key(start_key, end_key)

        cached = cache.get(cache_key)
        if cached is not None:
            logger.debug("Route cache hit for %s -> %s", start_key, end_key)
            return RouteResult(
                distance_miles=cached["distance_miles"],
                duration_seconds=cached["duration_seconds"],
                encoded_polyline=cached["encoded_polyline"],
                steps=_deserialize_steps(cached["steps"]),
                api_calls_used=0,
            )

        start_coord = _parse_coordinate(start)
        end_coord = _parse_coordinate(end)

        logger.info("[DEBUG] Parsed Start (lat,lon): %s", start_coord)
        logger.info("[DEBUG] Parsed End (lat,lon): %s", end_coord)

        budget = ORSCallBudget()
        route = self._fetch_directions(start_coord, end_coord, budget)

        cache.set(
            cache_key,
            {
                "distance_miles": route.distance_miles,
                "duration_seconds": route.duration_seconds,
                "encoded_polyline": route.encoded_polyline,
                "steps": _serialize_steps(route.steps),
            },
            timeout=self.cache_ttl,
        )

        return RouteResult(
            distance_miles=route.distance_miles,
            duration_seconds=route.duration_seconds,
            encoded_polyline=route.encoded_polyline,
            steps=route.steps,
            api_calls_used=budget.used,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_directions(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        budget: ORSCallBudget,
    ) -> RouteResult:
        start_lat, start_lon = start
        end_lat, end_lon = end

        payload = {
            "coordinates": [[start_lon, start_lat], [end_lon, end_lat]],
            "instructions": True,
            "units": "mi",
            "geometry": True,
            "radiuses": [5000, 5000],  # Max search radius (5km) to find routable points
        }
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

        logger.info("[DEBUG] Final ORS URL: %s", self.DIRECTIONS_URL)
        logger.info("[DEBUG] Final ORS Payload (order is [lon, lat]): %s", payload)

        data = self._request_with_retry(
            "POST",
            self.DIRECTIONS_URL,
            budget,
            json=payload,
            headers=headers,
        )

        return _parse_directions_response(data)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        budget: ORSCallBudget,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute one HTTP request (budget allows exactly 1 call)."""
        budget.consume()  # raises ORSCallBudgetExceeded if already used
        last_error: Exception | None = None
        attempts = self.max_retries + 1

        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
                response.raise_for_status()
                return response.json()
            except ORSCallBudgetExceeded:
                raise
            except requests.RequestException as exc:
                last_error = exc
                # Try to extract detailed error message from response body
                error_detail = ""
                if hasattr(exc, "response") and exc.response is not None:
                    try:
                        error_detail = f" | Detail: {exc.response.text}"
                    except Exception:
                        pass

                logger.warning(
                    "ORS request failed (attempt %s/%s): %s%s",
                    attempt + 1,
                    attempts,
                    exc,
                    error_detail,
                )
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))

        raise RouteServiceError(
            f"ORS request failed after {attempts} attempt(s)"
        ) from last_error


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _parse_coordinate(location: str) -> tuple[float, float]:
    """Parse a 'lat,lon' string. Raises InvalidCoordinateError on failure."""
    match = COORDINATE_PATTERN.match(location)
    if not match:
        raise InvalidCoordinateError(
            f"Expected 'lat,lon' coordinate string, got: {location!r}. "
            "Geocoding is disabled — please supply pre-resolved coordinates."
        )
    return float(match.group(1)), float(match.group(2))


def _normalize_coord_key(location: str) -> str:
    return " ".join(location.strip().lower().split())


def _route_cache_key(start: str, end: str) -> str:
    raw = f"{start}|{end}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"route:{digest}"


def _parse_directions_response(data: dict[str, Any]) -> RouteResult:
    routes = data.get("routes") or []
    if not routes:
        raise RouteServiceError("ORS returned no routes")

    route = routes[0]
    summary = route.get("summary") or {}

    distance_miles = float(summary.get("distance", 0))
    duration_seconds = float(summary.get("duration", 0))

    geometry = route.get("geometry")
    if not isinstance(geometry, str):
        raise RouteServiceError(f"ORS returned unexpected geometry type: {type(geometry)}")

    encoded_polyline = geometry
    steps = _parse_steps(route, distance_miles)

    return RouteResult(
        distance_miles=distance_miles,
        duration_seconds=duration_seconds,
        encoded_polyline=encoded_polyline,
        steps=steps,
        api_calls_used=0,
    )


def _parse_steps(
    properties: dict[str, Any], total_miles: float
) -> tuple[RouteStep, ...]:
    parsed: list[RouteStep] = []
    cumulative = 0.0

    for segment in properties.get("segments") or []:
        for step in segment.get("steps") or []:
            step_miles = float(step.get("distance", 0))
            midpoint = cumulative + (step_miles / 2)
            parsed.append(
                RouteStep(
                    name=(step.get("name") or "").strip(),
                    distance_miles=step_miles,
                    cumulative_miles=midpoint,
                )
            )
            cumulative += step_miles

    if not parsed and total_miles > 0:
        parsed.append(
            RouteStep(
                name="route",
                distance_miles=total_miles,
                cumulative_miles=total_miles / 2,
            )
        )

    return tuple(parsed)


def _serialize_steps(steps: tuple[RouteStep, ...]) -> list[dict[str, float | str]]:
    return [
        {
            "name": step.name,
            "distance_miles": step.distance_miles,
            "cumulative_miles": step.cumulative_miles,
        }
        for step in steps
    ]


def _deserialize_steps(
    items: list[dict[str, float | str]],
) -> tuple[RouteStep, ...]:
    return tuple(
        RouteStep(
            name=str(item["name"]),
            distance_miles=float(item["distance_miles"]),
            cumulative_miles=float(item["cumulative_miles"]),
        )
        for item in items
    )
