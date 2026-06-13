"""Polyline encoding/decoding and route geometry helpers."""

from __future__ import annotations

import math
from typing import Iterable


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google-encoded polyline into (latitude, longitude) pairs."""
    if not encoded:
        return []

    factor = 10**precision
    coordinates: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lng = 0

    while index < len(encoded):
        lat, index = _decode_value(encoded, index, lat)
        lng, index = _decode_value(encoded, index, lng)
        coordinates.append((lat / factor, lng / factor))

    return coordinates


def encode_polyline(coordinates: Iterable[tuple[float, float]], precision: int = 5) -> str:
    """Encode (latitude, longitude) pairs into a Google-encoded polyline."""
    factor = 10**precision
    output: list[str] = []
    prev_lat = 0
    prev_lng = 0

    for lat, lng in coordinates:
        lat_i = round(lat * factor)
        lng_i = round(lng * factor)
        output.append(_encode_value(lat_i - prev_lat))
        output.append(_encode_value(lng_i - prev_lng))
        prev_lat = lat_i
        prev_lng = lng_i

    return "".join(output)


def haversine_miles(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """Great-circle distance in miles between two WGS84 points."""
    radius_miles = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius_miles * math.asin(math.sqrt(a))


def cumulative_distances_miles(points: list[tuple[float, float]]) -> list[float]:
    """Return cumulative distance from the first point along a polyline."""
    if not points:
        return []

    cumulative = [0.0]
    for i in range(1, len(points)):
        lat1, lon1 = points[i - 1]
        lat2, lon2 = points[i]
        cumulative.append(cumulative[-1] + haversine_miles(lat1, lon1, lat2, lon2))

    return cumulative


def point_to_polyline_distance_miles(
    lat: float,
    lon: float,
    polyline: list[tuple[float, float]],
) -> tuple[float, float]:
    """
    Minimum distance from a point to a polyline.

    Returns (distance_miles, route_position_miles) where route_position_miles is
    the distance along the polyline to the closest projection point.
    """
    if len(polyline) < 2:
        return float("inf"), 0.0

    cumulative = cumulative_distances_miles(polyline)
    best_distance = float("inf")
    best_position = 0.0

    for i in range(len(polyline) - 1):
        lat1, lon1 = polyline[i]
        lat2, lon2 = polyline[i + 1]
        fraction, seg_distance = _point_to_segment(lat, lon, lat1, lon1, lat2, lon2)
        position = cumulative[i] + fraction * seg_distance

        proj_lat = lat1 + fraction * (lat2 - lat1)
        proj_lon = lon1 + fraction * (lon2 - lon1)
        distance = haversine_miles(lat, lon, proj_lat, proj_lon)

        if distance < best_distance:
            best_distance = distance
            best_position = position

    return best_distance, best_position


def _decode_value(encoded: str, index: int, previous: int) -> tuple[int, int]:
    shift = 0
    result = 0

    while True:
        byte = ord(encoded[index]) - 63
        index += 1
        result |= (byte & 0x1F) << shift
        shift += 5
        if byte < 0x20:
            break

    delta = ~(result >> 1) if result & 1 else (result >> 1)
    return previous + delta, index


def _encode_value(value: int) -> str:
    value = ~(value << 1) if value < 0 else (value << 1)
    chunks: list[str] = []

    while value >= 0x20:
        chunks.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5

    chunks.append(chr(value + 63))
    return "".join(chunks)


def _point_to_segment(
    lat: float,
    lon: float,
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> tuple[float, float]:
    """Return (fraction along segment, segment length in miles)."""
    segment_length = haversine_miles(lat1, lon1, lat2, lon2)
    if segment_length == 0:
        return 0.0, 0.0

    # Planar approximation is sufficient for short road segments.
    x, y = lon, lat
    x1, y1 = lon1, lat1
    x2, y2 = lon2, lat2
    dx, dy = x2 - x1, y2 - y1
    denominator = dx * dx + dy * dy

    if denominator == 0:
        return 0.0, segment_length

    fraction = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / denominator))
    return fraction, segment_length
