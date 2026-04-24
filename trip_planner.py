"""
trip_planner.py
---------------
Neighborhood expansion trip planner for the Bangkok transit chatbot.

Given a starting station and a hop radius, this module:
  1. Queries Neo4j for all stations reachable within N hops
     (crossing NEXT_TO and CONNECTS_TO relationships)
  2. Collects all nearby places from those stations
  3. Asks the LLM to format the results as an itinerary grouped by category

Usage from terminal_chat.py:
    import trip_planner
    plan = trip_planner.plan_trip(driver, "MRT_SAMYAN", hops=2, category=None)
    print(plan)
"""

import json
from collections import defaultdict

import llm_manager

# ---------------------------------------------------------------------------
# Category display order for the output
# ---------------------------------------------------------------------------

CATEGORY_ORDER = [
    "Department Store",
    "Tourist Attractions",
    "Hotel",
    "Hospital",
    "Education Center",
]

# ---------------------------------------------------------------------------
# Neo4j queries
# ---------------------------------------------------------------------------

# NOTE: Neo4j does not allow parameters inside variable-length path syntax (*0..$hops).
# The hop count must be a literal integer embedded in the query string.
# _NEIGHBORHOOD_QUERY is therefore a template — use _build_neighborhood_query(hops) below.

_NEIGHBORHOOD_QUERY_TEMPLATE = """
MATCH (origin:Station {{station_id: $origin_id}})
MATCH (origin)-[:NEXT_TO|CONNECTS_TO*0..{hops}]-(nearby:Station)
WITH origin, collect(DISTINCT nearby) AS reachable
UNWIND reachable AS station
MATCH (station)-[r:NEAR]->(p:Place)-[:BELONGS_TO]->(c:Category)
RETURN
    station.station_id  AS station_id,
    station.name_en     AS station_name,
    station.name_th     AS station_name_th,
    station.line_id     AS line_id,
    station.sequence_no AS sequence_no,
    p.name              AS place_name,
    p.name_th           AS place_name_th,
    c.name              AS category,
    r.distance_m        AS distance_m,
    (station.station_id = $origin_id) AS is_origin
ORDER BY station.sequence_no, r.distance_m
"""


def _build_neighborhood_query(hops: int) -> str:
    """Return the neighborhood Cypher query with hops baked in as a literal."""
    if hops < 1 or hops > 10:
        raise ValueError(f"hops must be between 1 and 10, got {hops}")
    return _NEIGHBORHOOD_QUERY_TEMPLATE.format(hops=hops)

# Fetch origin station details for the header
_ORIGIN_QUERY = """
MATCH (s:Station {station_id: $station_id})
RETURN s.name_en AS name_en, s.name_th AS name_th, s.line_id AS line_id
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_trip(
    driver,
    origin_station_id: str,
    hops: int = 2,
    category: str | None = None,
) -> str:
    """
    Build a trip plan from a given station.

    Args:
        driver:             Active Neo4j driver.
        origin_station_id:  Exact station_id e.g. 'MRT_SAMYAN'.
        hops:               Number of stops to expand in each direction (default 2).
        category:           If given, filter to one category only e.g. 'Hotel'.

    Returns:
        A formatted string ready to print in the terminal.
    """
    # 1. Validate origin
    origin_info = _get_origin(driver, origin_station_id)
    if not origin_info:
        return (
            f"  Station '{origin_station_id}' not found in the graph.\n"
            f"  Use /check to find the correct station ID."
        )

    # 2. Fetch neighborhood places
    places = _fetch_neighborhood(driver, origin_station_id, hops)
    if not places:
        return (
            f"  No places found within {hops} stops of {origin_info['name_en']}.\n"
            f"  Try increasing the hop radius: /plan {origin_station_id} 3"
        )

    # 3. Optional category filter
    if category:
        places = [p for p in places if p["category"].lower() == category.lower()]
        if not places:
            return (
                f"  No '{category}' places found within {hops} stops of "
                f"{origin_info['name_en']}."
            )

    # 4. Ask LLM to format the trip plan
    return _format_plan(origin_info, places, hops, category)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_origin(driver, station_id: str) -> dict | None:
    with driver.session() as session:
        result = session.run(_ORIGIN_QUERY, station_id=station_id)
        row = result.single()
        return dict(row) if row else None


def _fetch_neighborhood(driver, origin_id: str, hops: int) -> list[dict]:
    query = _build_neighborhood_query(hops)
    with driver.session() as session:
        result = session.run(query, origin_id=origin_id)
        return [dict(r) for r in result]


def _group_places(places: list[dict]) -> dict:
    """
    Group places by category, then by station within each category.
    Returns:
        {
          "Department Store": {
              "MRT_SAMYAN": [{"place_name": ..., "distance_m": ..., ...}, ...]
          },
          ...
        }
    """
    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for p in places:
        cat = p["category"]
        sid = p["station_id"]
        grouped[cat][sid].append(p)
    return grouped


def _build_context(origin_info: dict, places: list[dict], hops: int, category: str | None) -> str:
    """Build the structured context string sent to the LLM."""
    grouped = _group_places(places)

    # Collect unique station names for the header
    station_ids = list(dict.fromkeys(p["station_id"] for p in places))  # preserve order
    station_labels = []
    for p in places:
        label = f"{p['station_name']} ({p['line_id'].replace('_', ' ').title()})"
        if label not in station_labels:
            station_labels.append(label)

    lines = [
        f"Origin: {origin_info['name_en']} / {origin_info['name_th']} ({origin_info['line_id']})",
        f"Hop radius: {hops} stops (includes interchange stations)",
        f"Stations covered: {', '.join(station_labels)}",
        "",
        "Places found (grouped by category and station):",
        "",
    ]

    for cat in CATEGORY_ORDER:
        if cat not in grouped:
            continue
        if category and cat.lower() != category.lower():
            continue

        lines.append(f"[{cat}]")
        for sid, place_list in grouped[cat].items():
            # find station display name
            station_display = next(
                (f"{p['station_name']} ({p['line_id']})" for p in place_list), sid
            )
            lines.append(f"  {station_display}:")
            for p in sorted(place_list, key=lambda x: x["distance_m"]):
                th = f" / {p['place_name_th']}" if p.get("place_name_th") else ""
                lines.append(f"    - {p['place_name']}{th}  ({p['distance_m']}m)")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM prompt for trip planning
# ---------------------------------------------------------------------------

_TRIP_SYSTEM = """
You are a Bangkok transit trip planner. Your job is to read a structured list of
places reachable from a starting station and write a helpful, friendly trip plan.

Rules:
- Write in the same language the user used (Thai or English).
- Group suggestions by category in this order:
    1. Department Store / shopping
    2. Tourist Attractions
    3. Hotel (if relevant)
    4. Hospital (only mention if asked or clearly relevant)
    5. Education Center (only mention if asked or clearly relevant)
- For each place mention its station and walking distance in metres.
- Highlight the origin station's places first, then nearby stations.
- Mention if a place is on a different line (means an interchange is needed).
- Keep the tone conversational and helpful — like a local friend giving advice.
- Do NOT mention Neo4j, Cypher, or any technical terms.
- End with a one-line tip about the area or the best time to visit.
"""


def _format_plan(
    origin_info: dict,
    places: list[dict],
    hops: int,
    category: str | None,
) -> str:
    """Send the structured place list to the LLM and return the formatted plan."""
    context = _build_context(origin_info, places, hops, category)

    category_note = f" (filtered to: {category})" if category else ""
    prompt = (
        f"Please write a trip plan{category_note} for someone starting at "
        f"{origin_info['name_en']} station.\n\n"
        f"Here are all the places available within {hops} stops:\n\n"
        f"{context}\n\n"
        f"Write the trip plan now."
    )

    return llm_manager.call(_TRIP_SYSTEM, prompt)


# ---------------------------------------------------------------------------
# Quick CLI test: python trip_planner.py MRT_SAMYAN 2
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import os
    from pathlib import Path
    from dotenv import load_dotenv
    from neo4j import GraphDatabase

    load_dotenv(Path(__file__).parent / ".env")

    neo4j_uri  = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER",     "neo4j")
    neo4j_pass = os.getenv("NEO4J_PASSWORD", "password")

    station_id = sys.argv[1] if len(sys.argv) > 1 else "MRT_SAMYAN"
    hops       = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    cat        = sys.argv[3] if len(sys.argv) > 3 else None

    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pass))
    print(plan_trip(driver, station_id, hops=hops, category=cat))
    driver.close()