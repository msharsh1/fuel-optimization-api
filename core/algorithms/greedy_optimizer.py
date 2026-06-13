"""Greedy fuel-stop optimizer with look-ahead: buy only enough to reach
a cheaper (or last-resort) station.

Fuel model
──────────
• Vehicle departs with a FULL tank = max_range miles of fuel.
• We only track and cost fuel PURCHASED at stations along the route.
• The pre-trip fuel already in the tank is not charged.
• gallons_purchased × price = stop cost.
• Total fuel consumed for the whole trip = total_distance / mpg.
• Total fuel purchased at stops = total consumed - fuel remaining from
  the starting full tank that was actually used.

Algorithm
─────────
At each decision point (current position):
  1. Find all stations reachable within remaining fuel.
  2. Look ahead: is there a CHEAPER station beyond the cheapest reachable
     one, still reachable after filling just enough at the near station?
  3. If cheaper station visible → buy MINIMAL fuel to just reach it.
  4. Otherwise → fill the tank completely at the cheapest reachable station.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from core.algorithms.route_projection import ProjectedStation

logger = logging.getLogger(__name__)

_EPSILON = 1e-6


class FuelOptimizationError(Exception):
    """Raised when no feasible fuel plan exists."""


# ── Data models ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FuelStop:
    station_id: int
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price: float           # $/gal
    route_miles: float     # position along route
    gallons: float         # gallons purchased at this stop
    fuel_cost_usd: float   # gallons × price


@dataclass(frozen=True)
class FuelPlan:
    stops: tuple[FuelStop, ...]
    total_fuel_cost_usd: float
    total_distance_miles: float


# ── Public entry-point ─────────────────────────────────────────────────────────


def optimize_fuel_stops(
    stations: list[ProjectedStation],
    total_distance_miles: float,
    *,
    max_range_miles: float | None = None,
    mpg: float | None = None,
) -> FuelPlan:
    """Return the lowest-cost fuel plan for the given projected station list.

    Parameters
    ----------
    stations:
        Stations projected onto the route (any order — sorted internally).
    total_distance_miles:
        Road distance of the full trip.
    max_range_miles:
        Miles of range from a full tank (default: settings.VEHICLE_MAX_RANGE_MILES).
    mpg:
        Fuel efficiency in miles-per-gallon (default: settings.VEHICLE_MPG).
    """
    if total_distance_miles <= 0:
        return FuelPlan(stops=(), total_fuel_cost_usd=0.0, total_distance_miles=0.0)

    max_range = max_range_miles if max_range_miles is not None else settings.VEHICLE_MAX_RANGE_MILES
    miles_per_gallon = mpg if mpg is not None else settings.VEHICLE_MPG

    # Sort by route position then price for tie-breaking
    ordered = sorted(stations, key=lambda s: (s.route_miles, s.price))

    # ── Short trip: entire route fits within one full tank ────────────────────
    if total_distance_miles <= max_range:
        if not ordered:
            # Start full, no purchase needed — total cost = 0 (fuel was pre-paid)
            return FuelPlan(
                stops=(),
                total_fuel_cost_usd=0.0,
                total_distance_miles=total_distance_miles,
            )
        # Buy all required fuel at the single cheapest station
        cheapest = min(ordered, key=lambda s: s.price)
        gallons = round(total_distance_miles / miles_per_gallon, 3)
        cost = round(gallons * cheapest.price, 2)
        stop = FuelStop(
            station_id=cheapest.station_id,
            name=cheapest.name,
            address=cheapest.address,
            city=cheapest.city,
            state=cheapest.state,
            price=cheapest.price,
            route_miles=cheapest.route_miles,
            gallons=gallons,
            fuel_cost_usd=cost,
        )
        return FuelPlan(
            stops=(stop,),
            total_fuel_cost_usd=cost,
            total_distance_miles=total_distance_miles,
        )

    # ── Long trip: greedy look-ahead ──────────────────────────────────────────
    if not ordered:
        raise FuelOptimizationError("No fuel stations available along the route.")

    current_pos = 0.0            # miles from start
    remaining_range = max_range  # miles of fuel remaining in tank (start full)
    total_cost = 0.0
    stops: list[FuelStop] = []
    visited: set[int] = set()    # guard against re-visiting the same station

    while True:
        remaining_trip = total_distance_miles - current_pos

        if remaining_trip <= remaining_range + _EPSILON:
            # Enough fuel to reach destination — done
            break

        reachable_limit = current_pos + remaining_range

        # Stations reachable from current position
        reachable = [
            s for s in ordered
            if current_pos + _EPSILON < s.route_miles <= reachable_limit
            and s.station_id not in visited
        ]

        if not reachable:
            raise FuelOptimizationError(
                f"No fuel station reachable within {remaining_range:.1f} miles "
                f"from mile {current_pos:.1f}."
            )

        # Cheapest reachable station
        best_local = min(reachable, key=lambda s: (s.price, s.route_miles))

        # Fuel remaining when we arrive at best_local
        range_at_stop = remaining_range - (best_local.route_miles - current_pos)

        # Look ahead: cheaper station reachable after topping up at best_local?
        lookahead_limit = best_local.route_miles + max_range
        cheaper_ahead = [
            s for s in ordered
            if s.route_miles > best_local.route_miles
            and s.route_miles <= lookahead_limit
            and s.price < best_local.price - _EPSILON
            and s.station_id not in visited
        ]

        if cheaper_ahead:
            # Minimal fill: buy exactly enough to reach the nearest cheaper station
            target = min(cheaper_ahead, key=lambda s: s.route_miles)
            miles_stop_to_target = target.route_miles - best_local.route_miles
            range_needed = miles_stop_to_target                       # miles needed in tank
            gallons_to_buy = max(0.0, (range_needed - range_at_stop) / miles_per_gallon)

            cost = round(gallons_to_buy * best_local.price, 2)

            if gallons_to_buy > _EPSILON:
                stops.append(
                    FuelStop(
                        station_id=best_local.station_id,
                        name=best_local.name,
                        address=best_local.address,
                        city=best_local.city,
                        state=best_local.state,
                        latitude=best_local.latitude,
                        longitude=best_local.longitude,
                        price=best_local.price,
                        route_miles=best_local.route_miles,
                        gallons=round(gallons_to_buy, 3),
                        fuel_cost_usd=cost,
                    )
                )
                total_cost += cost

            # Update state: drove to best_local, bought gallons_to_buy there
            new_range = range_at_stop + gallons_to_buy * miles_per_gallon
            remaining_range = new_range
            visited.add(best_local.station_id)
            current_pos = best_local.route_miles

        else:
            # No cheaper option — fill the tank completely at best_local
            # gallons needed to fill: (max_range - range_at_stop) / 1
            # NOTE: range_at_stop is in miles; converting to gallons:
            gallons_to_fill = (max_range - range_at_stop) / miles_per_gallon
            cost = round(gallons_to_fill * best_local.price, 2)

            if gallons_to_fill > _EPSILON:
                stops.append(
                    FuelStop(
                        station_id=best_local.station_id,
                        name=best_local.name,
                        address=best_local.address,
                        city=best_local.city,
                        state=best_local.state,
                        latitude=best_local.latitude,
                        longitude=best_local.longitude,
                        price=best_local.price,
                        route_miles=best_local.route_miles,
                        gallons=round(gallons_to_fill, 3),
                        fuel_cost_usd=cost,
                    )
                )
                total_cost += cost

            remaining_range = max_range  # tank is now full
            visited.add(best_local.station_id)
            current_pos = best_local.route_miles

    return FuelPlan(
        stops=tuple(stops),
        total_fuel_cost_usd=round(total_cost, 2),
        total_distance_miles=total_distance_miles,
    )
