"""API views for the fuel optimization service."""

from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from core.algorithms.greedy_optimizer import FuelOptimizationError
from core.services.route_service import InvalidCoordinateError, RouteServiceError
from core.services.trip_service import TripService

logger = logging.getLogger(__name__)


class DashboardView(View):
    """Serve the interactive map dashboard."""

    def get(self, request, *args, **kwargs):
        return render(request, "core/index.html")


@method_decorator(csrf_exempt, name="dispatch")
class OptimizeRouteView(View):
    """POST /api/optimize/

    Accepts 'lat,lon' coordinates or 'City, ST' city names (resolved offline).

    Request body (JSON):
        {
            "start": "32.7767,-96.7970",   // lat,lon  OR  "Dallas, TX"
            "end":   "35.4676,-97.5164"    // lat,lon  OR  "Oklahoma City, OK"
        }

    Response (JSON):
        {
            "route": {
                "distance": <float, miles>,
                "duration": <float, seconds>,
                "polyline": <encoded polyline string>
            },
            "fuel_stops": [
                {
                    "name":    <station name>,
                    "city":    <city>,
                    "state":   <state abbreviation>,
                    "address": <address>,
                    "price":   <$/gallon>,
                    "gallons": <gallons purchased>,
                    "cost":    <gallons × price>,
                    "route_miles": <miles along route>
                }
            ],
            "total_cost": <total fuel cost in USD>
        }
    """

    def post(self, request, *args, **kwargs):
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body."}, status=400)

        start = (body.get("start") or "").strip()
        end = (body.get("end") or "").strip()

        if not start or not end:
            return JsonResponse(
                {"error": "Both 'start' and 'end' fields are required."},
                status=400,
            )

        try:
            result = TripService().plan_trip_as_dict(start, end)
            return JsonResponse(result, status=200)

        except ValueError as exc:
            # City name not found in dataset, or unresolvable input
            return JsonResponse({"error": str(exc)}, status=400)

        except InvalidCoordinateError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        except FuelOptimizationError as exc:
            return JsonResponse({"error": str(exc)}, status=422)

        except RouteServiceError as exc:
            logger.error("ORS route error: %s", exc)
            return JsonResponse(
                {"error": "Route service error. Check ORS API key and coordinates."},
                status=502,
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in OptimizeRouteView: %s", exc)
            return JsonResponse({"error": "Internal server error."}, status=500)
