# Hybrid Fuel Optimization System

A high-performance, route-aware fuel optimization system that minimizes fuel cost for long-haul trips using a **single external API call** and intelligent local computation.

---

## Overview

This project computes the **most cost-efficient fuel stops** between two locations by combining:

* Real-world routing data (via OpenRouteService)
* Spatial filtering of fuel stations
* A greedy look-ahead optimization algorithm

It is designed to be **API-efficient and demo-ready** with a full interactive dashboard served via Django.

---

## Tech Stack

### Backend (and Frontend Rendering)

* **Framework:** Django (Python 3)
* **Database:** SQLite (Fuel station dataset via Django ORM)
* **Caching:** Django LocMemCache (in-memory, 24h TTL)
* **Routing API:** OpenRouteService (ORS) Directions API
  *Strictly limited to 1 API call per request*

---

### Frontend

* **Rendering:** Django Templates (HTML + JavaScript)
* **Styling:** Tailwind CSS (glassmorphic dark UI via CDN)
* **Mapping:** Leaflet.js

---

## System Architecture

### Controller Layer

* `OptimizeRouteView`

  * Handles incoming API requests
  * Manages caching using slugified keys

---

### Orchestration Layer

* `TripService`

  * Core coordinator of the entire pipeline

---

### Data Layer

* `CityIndex`

  * In-memory datastore
  * Resolves city names → `(lat, lon)` without external APIs
  * Uses median-based filtering for noisy data

---

### External Integration

* `RouteService`

  * Makes **exactly ONE** request to ORS
  * Returns:

    * Distance
    * Encoded polyline

---

### Spatial Processing

* `RouteProjection`

  * Decodes polyline
  * Creates route checkpoints (~every 40 miles)
  * Filters stations within a **15-mile corridor**

---

### Optimization Engine

* `GreedyOptimizer`

  * Determines optimal fuel stops using a **look-ahead strategy**

---

## Data Flow

1. User enters start & destination (UI)
2. POST request → `/api/optimize/`
3. Cache check (LocMemCache)
4. City resolution via `CityIndex`
5. Route fetched from ORS (1 API call)
6. Polyline decoded into coordinates
7. Stations filtered near route
8. Greedy optimization applied
9. Response cached (TTL: 24h)
10. JSON returned to frontend
11. UI renders:

* Route polyline
* Fuel stop markers
* Cost summary

---

## Optimization Algorithm

### Greedy Look-Ahead Strategy

At each fuel station:

1. Identify all reachable stations with current fuel
2. Find the cheapest among them
3. Look ahead:

   * If a cheaper station exists → buy minimum fuel to reach it
   * Otherwise → fill full tank

---

## Key Design Decisions

### Single API Call Constraint

* Entire route computed using only **one ORS request**

---

### Median-Based Location Resolution

* Handles noisy dataset
* Filters out invalid station coordinates (>500 miles from state center)

---

### Spatial Filtering

* Only considers stations within a **15-mile corridor of the route**

---

### Caching Strategy

* Uses in-memory cache
* Key format:

  ```
  route_<start_slug>_<end_slug>
  ```
* TTL: 24 hours

---

## Features

* Interactive map (Leaflet)
* Route visualization (decoded polyline)
* Fuel stop markers with pricing
* Trip summary:

  * Distance
  * Total cost
* Scrollable fuel stop list
* Popup insights on each station

---

## How to Run

### 1. Clone Repository

```bash
git clone https://github.com/msharsh1/fuel-optimization-api
cd fuel-optimization-api
```

---

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

---

### 3. Run Server

```bash
python manage.py runserver
```

---

### 4. Open Application

```
http://127.0.0.1:8000/
```

---

## API Testing (Postman)

**Request Type:** POST
**URL:**

```
http://127.0.0.1:8000/api/optimize/
```

**Headers:**

```
Content-Type: application/json
Authorization: (Kepp it empty)
```

**Body (JSON):**

```json
{
  "start": "Lupton, AZ",
  "end": "Seguin, TX"
}
```

---

## Notes

* Ensure your ORS API key is valid
* CORS must be enabled if testing externally
* Cache resets on server restart

---

## Future Improvements

* Persistent caching (Redis)
* Real-time fuel price APIs
* Multi-vehicle optimization
* UI enhancements (animations, analytics)
* Deployment (Docker + Cloud)

---

## License

This project is for academic and demonstration purposes.

---
