"""
fare.py
-------
Bangkok transit fare calculator for BTS and MRT lines.

Fares are distance-based (number of stops travelled).
Update the tables below whenever fares change — no other file needs touching.

Current rates (last verified April 2026):
  MRT Blue Line   — effective 3 July 2024 to 2 July 2026
  BTS Sukhumvit   — effective 1 November 2025
  BTS Silom       — effective 1 November 2025 (same table as Sukhumvit)
"""

# Fare tables
# MRT Blue Line
# Source: Cabinet approval June 2024, effective July 3 2024
# 17 ฿ base + increments per stop, capped at 45 ฿ for 12+ stops
_MRT_BLUE_FARES = [
    0,   # 0 stops (unused)
    17,  # 1 stop
    20,  # 2 stops
    22,  # 3 stops
    25,  # 4 stops
    27,  # 5 stops
    30,  # 6 stops
    32,  # 7 stops
    35,  # 8 stops
    37,  # 9 stops
    40,  # 10 stops
    42,  # 11 stops
    45,  # 12+ stops (cap)
]

# BTS Sukhumvit Line and BTS Silom Line share one fare table
# Source: BTS official, effective 1 November 2025
# 17 ฿ base, capped at 65 ฿
_BTS_FARES = [
    0,   # 0 stops (unused)
    17,  # 1 stop
    23,  # 2 stops
    26,  # 3 stops
    30,  # 4 stops
    33,  # 5 stops
    37,  # 6 stops
    40,  # 7 stops
    44,  # 8 stops
    47,  # 9 stops
    51,  # 10 stops
    54,  # 11 stops
    58,  # 12 stops
    61,  # 13 stops
    65,  # 14+ stops (cap)
]

# Map line_id values (from your Neo4j graph) to their fare table
_FARE_TABLES = {
    "MRT_BLUE":      _MRT_BLUE_FARES,
    "BTS_SUKHUMVIT": _BTS_FARES,
    "BTS_SILOM":     _BTS_FARES,
}

# ---------------------------------------------------------------------------
# Interchange surcharge
# BTS and MRT are separate operators — no cross-system discount.
# When a journey crosses lines, both fares are calculated independently
# from zero and summed.
# ---------------------------------------------------------------------------

INTERCHANGE_SURCHARGE = 0   # set to non-zero if a surcharge is introduced


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fare_for_stops(stops: int, line_id: str) -> int:
    """
    Return the single-journey fare in Thai Baht for travelling `stops` stations
    on the given line.

    Args:
        stops:   Number of stations travelled (must be >= 1).
        line_id: One of "MRT_BLUE", "BTS_SUKHUMVIT", "BTS_SILOM".

    Returns:
        Fare in Baht (int).

    Raises:
        ValueError: If line_id is not recognised or stops < 1.
    """
    if stops < 1:
        raise ValueError(f"stops must be >= 1, got {stops}")

    table = _FARE_TABLES.get(line_id)
    if table is None:
        raise ValueError(
            f"Unknown line_id '{line_id}'. "
            f"Valid options: {list(_FARE_TABLES.keys())}"
        )

    # Clamp to last entry for journeys beyond the table
    index = min(stops, len(table) - 1)
    return table[index]


def calculate_journey_fare(path: list[dict]) -> dict:
    """
    Calculate the total fare for a multi-station journey.

    Args:
        path: List of station dicts, each with at least:
              {"station_id": str, "name_en": str, "line_id": str}
              Ordered from origin to destination.

    Returns:
        {
          "total_fare":   int,           total Baht
          "segments":     [              one entry per continuous line segment
            {
              "line_id":  str,
              "from":     str,           station name
              "to":       str,           station name
              "stops":    int,
              "fare":     int,
            }
          ],
          "interchanges": int            number of line changes
        }
    """
    if len(path) < 2:
        return {"total_fare": 0, "segments": [], "interchanges": 0}

    segments = []
    interchanges = 0

    seg_start = 0
    current_line = path[0]["line_id"]

    for i in range(1, len(path)):
        station = path[i]
        line = station["line_id"]

        # Line change detected OR last station
        if line != current_line or i == len(path) - 1:
            seg_end = i if line == current_line else i - 1
            seg_stops = seg_end - seg_start
            seg_fare = fare_for_stops(seg_stops, current_line) if seg_stops >= 1 else 0

            segments.append({
                "line_id": current_line,
                "from":    path[seg_start]["name_en"],
                "to":      path[seg_end]["name_en"],
                "stops":   seg_stops,
                "fare":    seg_fare,
            })

            if line != current_line:
                interchanges += 1
                # Start new segment from the interchange station
                seg_start = i - 1
                current_line = line

                # Handle the case where this is also the last station
                if i == len(path) - 1:
                    segments.append({
                        "line_id": current_line,
                        "from":    path[seg_start]["name_en"],
                        "to":      path[i]["name_en"],
                        "stops":   1,
                        "fare":    fare_for_stops(1, current_line),
                    })

    total = sum(s["fare"] for s in segments) + INTERCHANGE_SURCHARGE * interchanges
    return {
        "total_fare":   total,
        "segments":     segments,
        "interchanges": interchanges,
    }


def fare_summary(path: list[dict]) -> str:
    """
    Return a human-readable fare summary string for use in chatbot answers.

    Example output:
      Fare: 17 ฿ (MRT Blue Line, 1 stop)
      Fare: 17 ฿ + 17 ฿ = 34 ฿ (MRT Blue 1 stop + BTS Silom 1 stop, 1 interchange)
    """
    result = calculate_journey_fare(path)

    if not result["segments"]:
        return "Fare: n/a"

    if len(result["segments"]) == 1:
        s = result["segments"][0]
        line_label = _line_label(s["line_id"])
        return f"Estimated fare: {s['fare']} ฿ ({line_label}, {s['stops']} stop{'s' if s['stops'] != 1 else ''})"

    parts = [f"{s['fare']} ฿ ({_line_label(s['line_id'])}, {s['stops']} stop{'s' if s['stops'] != 1 else ''})"
             for s in result["segments"]]
    interchange_note = f"{result['interchanges']} interchange{'s' if result['interchanges'] != 1 else ''}"
    return f"Estimated fare: {' + '.join(parts)} = {result['total_fare']} ฿ ({interchange_note})"


def _line_label(line_id: str) -> str:
    return {
        "MRT_BLUE":      "MRT Blue Line",
        "BTS_SUKHUMVIT": "BTS Sukhumvit Line",
        "BTS_SILOM":     "BTS Silom Line",
    }.get(line_id, line_id)


# ---------------------------------------------------------------------------
# Quick test (run: python fare.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Fare table spot checks ===")
    tests = [
        (1,  "MRT_BLUE",      17),
        (5,  "MRT_BLUE",      27),
        (12, "MRT_BLUE",      45),
        (20, "MRT_BLUE",      45),
        (1,  "BTS_SUKHUMVIT", 17),
        (5,  "BTS_SUKHUMVIT", 33),
        (14, "BTS_SUKHUMVIT", 65),
        (1,  "BTS_SILOM",     17),
    ]
    all_ok = True
    for stops, line, expected in tests:
        got = fare_for_stops(stops, line)
        status = "✓" if got == expected else f"✗ (expected {expected})"
        print(f"  {line:20s}  {stops:2d} stop(s) → {got:2d} ฿  {status}")
        if got != expected:
            all_ok = False

    print()
    print("=== Sample journey: MRT Samyan → BTS Siam (1 interchange) ===")
    sample_path = [
        {"station_id": "MRT_SAMYAN",        "name_en": "SAMYAN",    "line_id": "MRT_BLUE"},
        {"station_id": "MRT_SILOM",          "name_en": "SILOM",     "line_id": "MRT_BLUE"},
        {"station_id": "BTS_SALA DAENG",     "name_en": "SALA DAENG","line_id": "BTS_SILOM"},
        {"station_id": "BTS_SILOM_SIAM",     "name_en": "SIAM",      "line_id": "BTS_SILOM"},
    ]
    print(" ", fare_summary(sample_path))
    print()
    print("All checks passed!" if all_ok else "Some checks FAILED — review fare tables.")