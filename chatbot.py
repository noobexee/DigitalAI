"""
terminal_chat.py
----------------
Interactive terminal chatbot for the Bangkok transit knowledge graph.

The LLM backend (Typhoon / Gemini) is configured in llm_manager.py.
Change the PROVIDER variable at the top of that file to switch models.

Usage
-----
  python terminal_chat.py

.env file (create in the same folder as this script)
  TYPHOON_API_KEY=your_key_here    ← if using Typhoon (default)
  GOOGLE_API_KEY=your_key_here     ← if using Gemini
  NEO4J_URI=bolt://localhost:7687
  NEO4J_USER=neo4j
  NEO4J_PASSWORD=your_password

Commands during chat
  /debug    toggle showing generated Cypher + raw results
  /check    list or search station IDs in the graph
  /plan     generate a trip plan from a station (see /help for syntax)
  /clear    clear the screen
  /help     show available commands
  /quit     exit  (also: /exit, Ctrl+C, Ctrl+D)
"""

import os
import sys
import json
import re
import logging
import textwrap
from pathlib import Path

from dotenv import load_dotenv
import llm_manager
import fare as fare_calc
import trip_planner
from neo4j import GraphDatabase
from neo4j.exceptions import CypherSyntaxError, ServiceUnavailable

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Silence neo4j INFO noise
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.WARNING)
logging.getLogger("neo4j").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

# ---------------------------------------------------------------------------
# ANSI colors (auto-disabled if terminal doesn't support them)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

def cyan(t):    return _c("96", t)
def green(t):   return _c("92", t)
def yellow(t):  return _c("93", t)
def dim(t):     return _c("2",  t)
def bold(t):    return _c("1",  t)
def red(t):     return _c("91", t)

# ---------------------------------------------------------------------------
# Graph schema — injected into every Claude prompt
# ---------------------------------------------------------------------------

SCHEMA_PROMPT = """
You are a Bangkok transit assistant. You answer questions about MRT and BTS train stations,
nearby places, and routes. You support both English and Thai — always reply in the same
language the user used.

You have access to a Neo4j graph database with the following schema:

Node types:
  Line      — line_id, name, color
  Station   — station_id, name_en, name_th, line_id, sequence_no
  Place     — place_id, name (English), name_th (Thai), category, distance_m
  Category  — name

Relationships:
  (Line)-[:HAS_STATION]->(Station)
  (Station)-[:NEXT_TO]->(Station)           bidirectional, same line, sequential
  (Station)-[:CONNECTS_TO]->(Station)       cross-line interchange
  (Station)-[:INLINE_CONNECTION]->(Station) non-sequential same-line link
  (Station)-[:NEAR {distance_m}]->(Place)
  (Place)-[:BELONGS_TO]->(Category)

Available categories (EXACTLY 5 — use these exact strings when filtering):
  "Hotel"               — hotels and accommodation
  "Department Store"    — shopping malls and department stores
  "Tourist Attractions" — temples, museums, aquariums, parks, and other attractions
  "Education Center"    — universities and schools
  "Hospital"            — hospitals and medical centers

Category mapping — when the user mentions these words, map to the correct category:
  mall, shopping, department store, central, terminal21  → "Department Store"
  temple, wat, museum, aquarium, park, zoo, gallery      → "Tourist Attractions"
  university, school, college, institute                 → "Education Center"
  hospital, clinic, medical center                       → "Hospital"
  hotel, accommodation, resort, inn, hostel              → "Hotel"

Category query rules:
  - Always match category with EXACT string e.g. c.name = 'Hotel'
  - For attraction sub-types (temple, museum, park etc.) also filter by name:
      WHERE c.name = 'Tourist Attractions' AND toLower(p.name) CONTAINS 'temple'
  - For education sub-types (university, school) also filter by name:
      WHERE c.name = 'Education Center' AND toLower(p.name) CONTAINS 'university'
  - Never invent or guess category names outside the 5 listed above.

CRITICAL — ALWAYS use station_id for lookups, NEVER guess or construct IDs:
  The station_id format is NOT predictable from the station name.
  Example of wrong guesses the model must NEVER make:
    BTS_SUKHUMVIT_ASOOK  ← WRONG (extra O, wrong prefix)
    BTS_ASOK_STATION     ← WRONG
    BTS_SUKHUMVIT_ASOK   ← WRONG
  The only correct ID is: BTS_ASOK

  Use this exact reference table. Copy the station_id character-for-character:

  --- MRT Blue Line ---
  MRT_LAK SONG | MRT_BANG KHAE | MRT_PHASI CHAROEN | MRT_PHETKASEM 48
  MRT_BANG WA | MRT_BANG PHAI | MRT_THA PHRA | MRT_ITSARAPHAP
  MRT_SANAM CHAI | MRT_SAM YOT | MRT_WAT MANGKON | MRT_HUA LAMPHONG
  MRT_SAMYAN | MRT_SILOM | MRT_LUMPHINI | MRT_KHLONG TOEI
  MRT_QUEEN SIRIKIT NATIONAL CONVENTION | MRT_SUKHUMVIT | MRT_PHETCHABURI
  MRT_RAMA 9 | MRT_THAILAND CULTURAL CENTRE | MRT_SUTTHISAN
  MRT_RATCHADAPHISEK | MRT_LAT PHRAO | MRT_PHAHONYOTHIN | MRT_CHATUCHAK
  MRT_KAMPHAENG PHET | MRT_BANG SUE | MRT_TAO POON | MRT_BANG PHO
  MRT_BANG O | MRT_BANG PHLAT | MRT_SIRINDHORN | MRT_BANG YI KHAN
  MRT_BANG KHUN NON | MRT_FAI CHAI | MRT_CHARAN 13

  --- BTS Sukhumvit Line ---
  BTS_KHU KHOT | BTS_YAEK KOR POR AOR | BTS_ROYAL THAI AIR FORCE MUSEUM
  BTS_BHUMIBOL ADULYADEJ HOSPITAL | BTS_SAPHAN MAI | BTS_SAI YUD
  BTS_PHAHON YOTHIN 59 | BTS_WAT PHRA SRI MAHATHAT | BTS_11TH INFANTRY REGIMENT
  BTS_BANG BUA | BTS_ROYAL FOREST DEPARTMENT | BTS_KASETSART UNIVERSITY
  BTS_SENA NIKHOM | BTS_RATCHAYOTHIN | BTS_PHAHON YOTHIN 24
  BTS_HA YAEK LAT PHRAO | BTS_MO CHIT | BTS_SAPHAN KHWAI | BTS_ARI
  BTS_SANAM PAO | BTS_VICTORY MONUMENT | BTS_PHAYA THAI | BTS_RATCHATHEWI
  BTS_SUKHUMVIT_SIAM | BTS_CHIT LOM | BTS_PHLOEN CHIT | BTS_NANA
  BTS_ASOK | BTS_PHROM PHONG | BTS_THONG LO | BTS_EKKAMAI
  BTS_PHRA KHANONG | BTS_ON NUT | BTS_BANG CHAK | BTS_PUNNAWITHI
  BTS_UDOM SUK | BTS_BANG NA | BTS_BEARING | BTS_SAMRONG
  BTS_PU CHAO | BTS_CHANG ERAWAN | BTS_ROYAL THAI NAVAL ACADEMY
  BTS_PAK NAM | BTS_SAI LUAT | BTS_KHEHA

  --- BTS Silom Line ---
  BTS_NATIONAL STADIUM | BTS_SILOM_SIAM | BTS_RATCHADAMRI
  BTS_SALA DAENG | BTS_CHONG NONSI | BTS_SAINT LOUIS | BTS_SURASAK
  BTS_SAPHAN TAKSIN | BTS_KRUNG THON BURI | BTS_WONGWIAN YAI
  BTS_PHO NIMIT | BTS_TALAT PHLU | BTS_WUTTHAKAT | BTS_BANG WA

  Interchange stations (cross-line):
  BTS_ASOK        ↔  MRT_SUKHUMVIT       (Asok/Sukhumvit interchange)
  BTS_SALA DAENG  ↔  MRT_SILOM           (Sala Daeng/Silom interchange)
  BTS_BANG WA     ↔  MRT_BANG WA         (Bang Wa interchange)
  BTS_MO CHIT     ↔  MRT_CHATUCHAK       (Mo Chit/Chatuchak interchange)
  BTS_SUKHUMVIT_SIAM ↔ BTS_SILOM_SIAM   (Siam interchange, same physical station)

CRITICAL — name_en values are stored in ALL CAPS in the database.
  ALWAYS prefer station_id over name_en lookups — it is always exact.
  If you must use name_en: WHERE toUpper(s.name_en) = toUpper('keyword')

General query rules:
  - MATCH only — never MERGE, CREATE, DELETE, SET.
  - Station name search (English) : WHERE toUpper(s.name_en) = toUpper('keyword')
  - Station name search (Thai)    : WHERE s.name_th CONTAINS 'keyword'
  - Place name search (Thai)      : WHERE p.name_th CONTAINS 'keyword'
  - Distance filter               : WHERE r.distance_m <= 500
  - Shortest path                 : shortestPath((a:Station)-[:NEXT_TO|CONNECTS_TO*]-(b:Station))
  - LIMIT 10 by default.

Example queries:

  -- Hotels near BTS Asok (always use station_id)
  MATCH (s:Station {station_id: 'BTS_ASOK'})-[r:NEAR]->(p:Place)-[:BELONGS_TO]->(c:Category)
  WHERE c.name = 'Hotel'
  RETURN p.name, p.name_th, r.distance_m ORDER BY r.distance_m LIMIT 10

  -- Department stores near Siam (Sukhumvit line)
  MATCH (s:Station {station_id: 'BTS_SUKHUMVIT_SIAM'})-[r:NEAR]->(p:Place)-[:BELONGS_TO]->(c:Category)
  WHERE c.name = 'Department Store'
  RETURN p.name, p.name_th, r.distance_m ORDER BY r.distance_m LIMIT 10

  -- Shortest path from MRT Samyan to BTS Siam
  MATCH (a:Station {station_id: 'MRT_SAMYAN'}), (b:Station {station_id: 'BTS_SUKHUMVIT_SIAM'})
  MATCH path = shortestPath((a)-[:NEXT_TO|CONNECTS_TO*]-(b))
  RETURN
    [n IN nodes(path) | n.station_id] AS station_ids,
    [n IN nodes(path) | n.name_en]    AS stations,
    [n IN nodes(path) | n.line_id]    AS line_ids,
    length(path) AS stops

  -- Shortest path between two stations
  MATCH (a:Station {station_id: 'BTS_MO CHIT'}), (b:Station {station_id: 'BTS_ASOK'})
  MATCH path = shortestPath((a)-[:NEXT_TO|CONNECTS_TO*]-(b))
  RETURN
    [n IN nodes(path) | n.station_id] AS station_ids,
    [n IN nodes(path) | n.name_en]    AS stations,
    [n IN nodes(path) | n.line_id]    AS line_ids,
    length(path) AS stops

  -- Temples near MRT Sam Yot
  MATCH (s:Station {station_id: 'MRT_SAM YOT'})-[r:NEAR]->(p:Place)-[:BELONGS_TO]->(c:Category)
  WHERE c.name = 'Tourist Attractions' AND toLower(p.name) CONTAINS 'temple'
  RETURN p.name, p.name_th, r.distance_m ORDER BY r.distance_m LIMIT 10

  -- All tourist attractions on BTS Silom line
  MATCH (l:Line {line_id:'BTS_SILOM'})-[:HAS_STATION]->(s:Station)-[r:NEAR]->(p:Place)-[:BELONGS_TO]->(c:Category)
  WHERE c.name = 'Tourist Attractions'
  RETURN s.name_en, p.name, p.name_th, r.distance_m ORDER BY r.distance_m LIMIT 10

  -- Hospitals on MRT Blue line
  MATCH (l:Line {line_id:'MRT_BLUE'})-[:HAS_STATION]->(s:Station)-[r:NEAR]->(p:Place)-[:BELONGS_TO]->(c:Category)
  WHERE c.name = 'Hospital'
  RETURN s.name_en, p.name, p.name_th, r.distance_m ORDER BY r.distance_m LIMIT 10

  -- Nearest hospital to a station (search 3 stops wide — hospitals are sparse so always expand)
  MATCH (origin:Station {station_id: 'MRT_SAMYAN'})-[:NEXT_TO|CONNECTS_TO*0..3]-(nearby:Station)
  WITH collect(DISTINCT nearby) AS stations
  UNWIND stations AS s
  MATCH (s)-[r:NEAR]->(p:Place)-[:BELONGS_TO]->(c:Category)
  WHERE c.name = 'Hospital'
  RETURN s.name_en AS station, p.name, p.name_th, r.distance_m
  ORDER BY r.distance_m LIMIT 5

  -- Thai station name search
  MATCH (s:Station)-[r:NEAR]->(p:Place)
  WHERE s.name_th CONTAINS 'อโศก'
  RETURN s.name_en, p.name, p.name_th, r.distance_m ORDER BY r.distance_m LIMIT 10
"""

# ---------------------------------------------------------------------------
# Cypher safety guard
# ---------------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(MERGE|CREATE|DELETE|DETACH|SET|REMOVE|DROP|CALL\s+apoc\.schema)\b",
    re.IGNORECASE,
)

def validate_cypher(query: str) -> tuple[bool, str]:
    if _FORBIDDEN.search(query):
        return False, "Write operation detected — rejected."
    if not re.search(r"\bMATCH\b", query, re.IGNORECASE):
        return False, "No MATCH clause found."
    return True, ""

# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

def run_cypher(driver, query: str, **params) -> list[dict]:
    with driver.session() as session:
        return [dict(r) for r in session.run(query, **params)]

# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_CYPHER_SYSTEM = SCHEMA_PROMPT + """
Your task: convert the user's question into a Cypher query for the Bangkok transit graph.

You MUST respond with a JSON object in this exact format — nothing else:
{"cypher": "MATCH ... RETURN ..."}

Or if the question cannot be answered from this graph:
{"cypher": "CANNOT_ANSWER"}

Rules:
- The JSON must be on a single line.
- No markdown, no explanation, no text outside the JSON object.
- The cypher value must be a valid read-only Cypher query starting with MATCH.
- Never use MERGE, CREATE, DELETE, or SET inside the cypher value.

EMERGENCY / URGENT queries (injured, sick, hurt, accident, shot, bleeding, chest pain, emergency):
- Search for hospitals across ALL stations within 3 stops, NOT just the named station.
- Use this exact pattern (do NOT use collect + [origin] — it causes a syntax error):
    MATCH (origin:Station {station_id: 'MRT_SAMYAN'})-[:NEXT_TO|CONNECTS_TO*0..3]-(nearby:Station)
    WITH collect(DISTINCT nearby) AS stations
    UNWIND stations AS s
    MATCH (s)-[r:NEAR]->(p:Place)-[:BELONGS_TO]->(c:Category)
    WHERE c.name = 'Hospital'
    RETURN s.name_en AS station, p.name, p.name_th, r.distance_m
    ORDER BY r.distance_m LIMIT 5
- The *0..3 range already includes the origin station (0 hops = the station itself).
- Always expand to at least 3 stops for hospital queries regardless of what the user specified.

IMPORTANT — these question types must return CANNOT_ANSWER (they are handled separately):
- "plan my trip", "plan a trip", "what should I do", "suggest places to visit",
  "I'm at X, what can I do", "help me plan", "วางแผนทริป", "แนะนำสถานที่"
  These are trip-planning requests — NOT route or place-lookup questions.
- Only return a Cypher query for: route finding, specific place lookup, station info,
  interchange questions, and category-based place searches.
"""

_ANSWER_SYSTEM = SCHEMA_PROMPT + """
Your task: answer the user's question using the provided query results.

General rules:
- Same language as the user (Thai or English).
- Be friendly and clear.
- No mention of Cypher, Neo4j, or technical terms.

CRITICAL — when query results are empty ([]):
- You MUST say that no results were found in the database.
- You MUST NOT invent, guess, or recall any place names, hospital names, hotel names,
  or any other information from your training data.
- Do NOT say things like "the nearest hospital is X" if X is not in the results.
- The only correct response to empty results is to say nothing was found and suggest
  the user try a nearby station or a wider search.
- Example of correct empty response: "ไม่พบโรงพยาบาลในฐานข้อมูลใกล้สถานีนี้
  ลองค้นหาจากสถานีใกล้เคียงดูได้นะคะ"

EMERGENCY / URGENT situations (user mentions injury, accident, pain, medical emergency):
- ALWAYS start the answer with the Thai emergency number: โทร 1669 (สายด่วนฉุกเฉิน)
- In English: Call 1669 (Emergency Medical Services) immediately.
- Then list the nearest hospitals from the results with their station and distance.
- Keep the answer short and actionable — this is urgent.
- Do NOT say "I'm sorry" or add disclaimers — just give the information fast.

For ROUTE / PATH questions (when results contain a list of stations):
- List EVERY station in the path — do not skip or summarize mid-route.
- For each interchange point (where the line changes), explicitly tell the user:
    "Exit at [station], transfer to [Line name], then take the train to [next station]"
- State the total number of stops.
- Mention the final destination clearly at the end.
- Example of a good route answer:
    1. Start at MRT Samyan (MRT Blue Line)
    2. Take the MRT Blue Line to MRT Silom (1 stop)
    3. At MRT Silom, exit and walk to BTS Sala Daeng (interchange)
    4. Take the BTS Silom Line to BTS Siam (2 stops)
    Total: 3 stops, 1 interchange

For "WHAT STATIONS ARE BETWEEN X AND Y" questions:
- List ALL intermediate stations from the verified data — do not skip any.
- Format as a numbered list showing station name and which line it is on.
- At the end show total stops and interchanges.
- Use the "Intermediate stations" field from the verified route data.

For PLACE / NEARBY questions (when results contain place names and distances):
- List all results with name and distance in metres.
- Sort from nearest to furthest in your answer.
"""

# ---------------------------------------------------------------------------
# Cypher extraction
# ---------------------------------------------------------------------------

def _extract_cypher(raw: str) -> str:
    """
    Extract a Cypher query from the LLM's JSON response.
    Expected format: {"cypher": "MATCH ..."}
    Falls back to scanning for MATCH if JSON parsing fails.
    """
    text = raw.strip()

    # Strip markdown code fences if present
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    # Try JSON parse (the happy path)
    try:
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            cypher = data.get("cypher", "").strip()
            if cypher:
                return cypher
    except (json.JSONDecodeError, KeyError):
        pass

    # Fallback: find first MATCH keyword
    if "CANNOT_ANSWER" in text.upper():
        return "CANNOT_ANSWER"

    match = re.search(r"\bMATCH\b", text, re.IGNORECASE)
    if match:
        return text[match.start():].strip()

    return text   # let the validator reject it with a clear error


# ---------------------------------------------------------------------------
# Pipeline functions — now use llm_manager.call()
# ---------------------------------------------------------------------------

def generate_cypher(question: str) -> str:
    """Translate a natural language question into a Cypher query."""
    raw = llm_manager.call(_CYPHER_SYSTEM, question)
    return _extract_cypher(raw)


def format_answer(question: str, cypher: str, results: list[dict], fare_info: str | None = None) -> str:
    """Turn raw Neo4j results into a natural language answer."""

    # Hard guard: when results are empty inject an explicit instruction so the
    # LLM cannot hallucinate place names from training data
    if not results:
        empty_guard = (
            "\n⚠️ IMPORTANT: The query returned NO results (empty list above). "
            "You MUST NOT mention any specific place, hospital, hotel, or location name. "
            "Do NOT use your training knowledge to fill in names. "
            "Only say that nothing was found in the database and suggest alternatives "
            "like searching nearby stations or widening the search.\n"
        )
    else:
        empty_guard = ""

    # Route info is pre-computed verified data — label it clearly so the LLM
    # uses it instead of counting stops itself
    route_section = f"\n{fare_info}\n" if fare_info else ""

    body = (
        f"User question: {question}\n\n"
        f"Cypher used:\n{cypher}\n\n"
        f"Results:\n{json.dumps(results, ensure_ascii=False, indent=2)}\n"
        f"{empty_guard}"
        f"{route_section}"
        "Please answer the user's question.\n"
        "If route data is provided above, use the EXACT stop counts and station names from it — "
        "do NOT count the stations yourself or guess."
    )
    return llm_manager.call(_ANSWER_SYSTEM, body)


# ---------------------------------------------------------------------------
# Trip planning intent detection
# ---------------------------------------------------------------------------

# Keywords that signal "I'm at X and want trip suggestions" — not a route query
_TRIP_INTENT_KEYWORDS = [
    "plan trip", "plan a trip", "plan my trip", "trip plan",
    "what to do", "things to do", "suggest", "recommendation",
    "วางแผน", "แนะนำ", "อยากไป", "จะไปที่ไหน", "ท่องเที่ยว",
]

# Place-name → station_id mapping for common landmarks users mention by name
# Add more entries here as needed
_LANDMARK_TO_STATION: dict[str, str] = {
    "samyan mitrtown":    "MRT_SAMYAN",
    "mitrtown":           "MRT_SAMYAN",
    "สามย่านมิตรทาวน์":  "MRT_SAMYAN",
    "siam paragon":       "BTS_SUKHUMVIT_SIAM",
    "paragon":            "BTS_SUKHUMVIT_SIAM",
    "siam center":        "BTS_SUKHUMVIT_SIAM",
    "mbk":                "MRT_SAM YOT",
    "chatuchak":          "MRT_CHATUCHAK",
    "jj market":          "MRT_CHATUCHAK",
    "terminal21":         "BTS_ASOK",
    "emquartier":         "BTS_PHROM PHONG",
    "emporium":           "BTS_PHROM PHONG",
    "asiatique":          "BTS_SAPHAN TAKSIN",
    "iconsiam":           "BTS_KRUNG THON BURI",
    "centralworld":       "BTS_CHIT LOM",
    "central world":      "BTS_CHIT LOM",
    "silom complex":      "MRT_SILOM",
    "lumpini park":       "MRT_LUMPHINI",
    "lumphini":           "MRT_LUMPHINI",
    "chinatown":          "MRT_WAT MANGKON",
    "yaowarat":           "MRT_WAT MANGKON",
    "วัดมังกร":          "MRT_WAT MANGKON",
    "grand palace":       "MRT_SANAM CHAI",
    "wat phra kaew":      "MRT_SANAM CHAI",
    "victory monument":   "BTS_VICTORY MONUMENT",
}


def _detect_trip_intent(question: str) -> tuple[bool, str | None]:
    """
    Detect if the user wants a trip plan rather than a route or place query.

    Returns:
        (is_trip_intent, station_id)
        station_id is None if intent detected but station could not be resolved.
    """
    q_lower = question.lower()

    # Check for trip planning keywords
    has_intent = any(kw in q_lower for kw in _TRIP_INTENT_KEYWORDS)
    if not has_intent:
        return False, None

    # Try to resolve a station from the question
    # 1. Check landmark → station mapping
    for landmark, station_id in _LANDMARK_TO_STATION.items():
        if landmark in q_lower:
            return True, station_id

    # 2. Check if a known station ID appears literally in the question (uppercased)
    q_upper = question.upper()
    known_prefixes = ["MRT_", "BTS_"]
    for prefix in known_prefixes:
        idx = q_upper.find(prefix)
        if idx != -1:
            # Extract the token (until space or end)
            token = q_upper[idx:].split()[0].rstrip(".,?!")
            return True, token

    # Intent detected but no station found — return True with None so
    # the caller can ask the user to specify
    return True, None


# ---------------------------------------------------------------------------
# Single question → answer
# ---------------------------------------------------------------------------

def ask(question: str, driver) -> dict:
    """Run one question through the full pipeline. Returns result dict."""

    # 0. Pre-flight: detect trip planning intent before hitting the LLM
    is_trip, station_id = _detect_trip_intent(question)
    if is_trip:
        if station_id is None:
            return {
                "answer": (
                    "ดูเหมือนคุณต้องการวางแผนทริป แต่ยังไม่ได้ระบุสถานีต้นทางนะคะ\n\n"
                    "It looks like you want to plan a trip — but which station are you starting from?\n"
                    "Use: /plan <station_id>  e.g. /plan MRT_SAMYAN\n"
                    "Use /check to find your station ID."
                ),
                "cypher": "",
                "results": [],
                "error": None,
            }

        print(dim(f"  Planning trip from {station_id}..."), end="\r", flush=True)
        try:
            plan_text = trip_planner.plan_trip(driver, station_id, hops=2)
            print(" " * 50, end="\r")
            return {
                "answer": plan_text,
                "cypher": f"[trip_planner: {station_id}, hops=2]",
                "results": [],
                "error": None,
            }
        except RuntimeError as e:
            return {"answer": str(e), "cypher": "", "results": [], "error": str(e)}

    # 1. Generate Cypher
    print(dim("  Thinking..."), end="\r", flush=True)
    try:
        cypher = generate_cypher(question)
    except RuntimeError as e:
        return {"answer": str(e), "cypher": "", "results": [], "error": str(e)}

    if cypher.strip().upper() == "CANNOT_ANSWER":
        return {
            "answer": (
                "ขอโทษนะคะ คำถามนี้อยู่นอกเหนือข้อมูลที่มีในระบบค่ะ\n"
                "Sorry, that question is outside the scope of the transit database.\n"
                "Try asking about BTS/MRT stations or nearby places."
            ),
            "cypher": cypher,
            "results": [],
            "error": None,
        }

    # 2. Safety check
    ok, reason = validate_cypher(cypher)
    if not ok:
        return {
            "answer": "Sorry, I couldn't generate a safe query. Please try rephrasing.",
            "cypher": cypher,
            "results": [],
            "error": reason,
        }

    # 3. Run query
    print(dim("  Querying graph..."), end="\r", flush=True)
    try:
        results = run_cypher(driver, cypher)
    except CypherSyntaxError as e:
        return {
            "answer": "I had trouble querying the database. Please rephrase your question.",
            "cypher": cypher,
            "results": [],
            "error": str(e),
        }
    except ServiceUnavailable:
        return {
            "answer": "Neo4j is unavailable. Please check that it is running.",
            "cypher": cypher,
            "results": [],
            "error": "ServiceUnavailable",
        }

    # 3b. If result looks like a path, compute verified stop counts + fare
    fare_info = _compute_route_info(results, driver)

    # 4. Format answer — inject fare info if available
    print(dim("  Composing answer..."), end="\r", flush=True)
    try:
        answer = format_answer(question, cypher, results, fare_info)
    except RuntimeError as e:
        return {"answer": str(e), "cypher": cypher, "results": results, "error": str(e)}

    return {"answer": answer, "cypher": cypher, "results": results, "error": None}


def _compute_route_info(results: list[dict], driver) -> str | None:
    """
    Build a verified route summary from shortest path query results.
    Expects results to contain: station_ids, stations (name_en), line_ids, stops.
    Uses data directly from the query — no second Neo4j lookup needed.
    """
    if not results:
        return None

    first = results[0]
    station_names = first.get("stations")
    if not station_names or not isinstance(station_names, list) or len(station_names) < 2:
        return None

    station_ids = first.get("station_ids", [])
    line_ids    = first.get("line_ids", [])

    # If the new columns aren't present (old-style query), fall back gracefully
    if not station_ids or not line_ids:
        return None

    # Build path from the three parallel lists — all same length
    path = [
        {"station_id": sid, "name_en": name, "line_id": lid}
        for sid, name, lid in zip(station_ids, station_names, line_ids)
    ]

    # Trim path at first occurrence of the destination name_en.
    # The Siam problem: shortestPath returns [..., BTS_SILOM_SIAM, BTS_SUKHUMVIT_SIAM]
    # because CONNECTS_TO links them. We stop at the first station whose name_en
    # matches the final station's name_en.
    dest_name = path[-1]["name_en"]
    for i, p in enumerate(path):
        if p["name_en"] == dest_name:
            path = path[:i + 1]
            break

    # Also deduplicate any remaining consecutive same-station_id nodes
    deduped = [path[0]]
    for p in path[1:]:
        if p["station_id"] != deduped[-1]["station_id"]:
            deduped.append(p)
    path = deduped

    total_stops = len(path) - 1

    # --- Segment computation ---
    # Walk path, collect segments. A segment is a contiguous run of stations
    # on the same line. The interchange is a physical walk between two stations
    # on different lines — it is NOT a stop.
    #
    # Example: ASOK(BTS) SUKHUMVIT(MRT) ... CHATUCHAK(MRT) MOHCHIT(BTS)
    #   Segment 1: BTS  ASOK→ASOK          0 stops  ← single-station segment, keep
    #   Interchange: walk from ASOK to SUKHUMVIT
    #   Segment 2: MRT  SUKHUMVIT→CHATUCHAK 8 stops
    #   Interchange: walk from CHATUCHAK to MOHCHIT
    #   Segment 3: BTS  MOHCHIT→MOHCHIT    0 stops  ← single-station segment, keep
    #
    # We keep single-station segments because they represent the origin/destination
    # stations at interchanges — needed for correct step generation.

    segments = []
    seg_start_idx = 0

    for i in range(1, len(path)):
        line_changed = path[i]["line_id"] != path[seg_start_idx]["line_id"]
        is_last = (i == len(path) - 1)

        if line_changed:
            # Close segment from seg_start to i-1
            segments.append({
                "line_id": path[seg_start_idx]["line_id"],
                "from":    path[seg_start_idx]["name_en"],
                "to":      path[i - 1]["name_en"],
                "stops":   (i - 1) - seg_start_idx,
            })
            seg_start_idx = i  # new segment starts at i

        if is_last:
            # Close the final segment
            segments.append({
                "line_id": path[seg_start_idx]["line_id"],
                "from":    path[seg_start_idx]["name_en"],
                "to":      path[i]["name_en"],
                "stops":   i - seg_start_idx,
            })

    # Count interchanges = number of line changes = segments - 1
    # but exclude zero-stop edge segments from the count for display purposes
    meaningful_segments = [s for s in segments if s["stops"] > 0]
    interchanges = max(0, len(meaningful_segments) - 1)

    # --- Fare ---
    try:
        fare_str = fare_calc.fare_summary(path)
    except Exception:
        fare_str = None

    # --- Step-by-step instructions using meaningful_segments only ---
    steps = [f"Step 1: Start at {path[0]['name_en']} ({fare_calc._line_label(path[0]['line_id'])})"]
    step = 2
    for seg_idx, seg in enumerate(meaningful_segments):
        line_label = fare_calc._line_label(seg["line_id"])
        stop_word  = "stop" if seg["stops"] == 1 else "stops"
        steps.append(
            f"Step {step}: Take {line_label} from {seg['from']} to {seg['to']} ({seg['stops']} {stop_word})"
        )
        step += 1
        if seg_idx < len(meaningful_segments) - 1:
            next_label = fare_calc._line_label(meaningful_segments[seg_idx + 1]["line_id"])
            steps.append(f"Step {step}: At {seg['to']}, exit and transfer to {next_label}")
            step += 1

    summary_line = f"Total: {total_stops} stop{'s' if total_stops != 1 else ''}, {interchanges} interchange{'s' if interchanges != 1 else ''}"

    # --- Build prompt injection ---
    intermediate = [p["name_en"] for p in path[1:-1]]  # stations between origin and dest
    lines = [
        "=== VERIFIED ROUTE DATA — copy these numbers exactly, do NOT recalculate ===",
        f"Origin:       {path[0]['name_en']}  ({fare_calc._line_label(path[0]['line_id'])})",
        f"Destination:  {path[-1]['name_en']} ({fare_calc._line_label(path[-1]['line_id'])})",
        f"Total stops:  {total_stops}",
        f"Interchanges: {interchanges}",
        f"All stations in order: {' → '.join(p['name_en'] for p in path)}",
        f"Intermediate stations (between origin and destination): {', '.join(intermediate) if intermediate else 'none'}",
        "",
        "Per-segment breakdown (only segments with stops > 0):",
    ]
    for seg in meaningful_segments:
        lines.append(
            f"  {fare_calc._line_label(seg['line_id'])}: "
            f"{seg['from']} → {seg['to']} = {seg['stops']} stop{'s' if seg['stops'] != 1 else ''}"
        )
    lines += ["", "Step-by-step (use verbatim):"]
    lines += [f"  {s}" for s in steps]
    lines.append(f"  {summary_line}")
    if fare_str:
        lines.append(f"  {fare_str}")
    lines.append("=== END VERIFIED ROUTE DATA ===")

    route_summary = "\n".join(lines)

    # Debug print — remove once verified correct
    print(dim("\n  [route_info]"))
    for l in lines:
        print(dim(f"  {l}"))
    print()

    return route_summary

# ---------------------------------------------------------------------------
# Terminal REPL
# ---------------------------------------------------------------------------

HELP_TEXT = f"""
{bold("Available commands")}
  {cyan("/debug")}                   — toggle showing generated Cypher and raw results
  {cyan("/check")}                   — show all station IDs currently in the graph
  {cyan("/check <word>")}            — filter stations by keyword  e.g. /check siam
  {cyan("/plan <station_id>")}       — trip plan: all places within 2 stops
  {cyan("/plan <station_id> <N>")}   — trip plan with custom hop radius  e.g. /plan MRT_SAMYAN 3
  {cyan("/plan <station_id> <N> <category>")} — filter by category
  {cyan("/clear")}                   — clear the screen
  {cyan("/help")}                    — show this message
  {cyan("/quit")}                    — exit the chatbot

{bold("Categories for /plan filter")}
  Hotel · Department Store · Tourist Attractions · Hospital · Education Center

{bold("Example questions")}
  What hotels are near BTS Asok?
  How do I get from MRT Samyan to BTS Siam?
  Tourist attractions near MRT Sam Yot
  Which stations connect MRT and BTS?
  โรงแรมใกล้ BTS อโศกมีอะไรบ้าง

{bold("Example /plan commands")}
  /plan MRT_SAMYAN
  /plan MRT_SAMYAN 3
  /plan BTS_ASOK 2 Hotel
  /plan BTS_SUKHUMVIT_SIAM 2 Department Store
"""

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def print_banner():
    print()
    print(bold(cyan("  Bangkok Transit Chatbot")))
    print(dim(f"  Model: {llm_manager.provider_name()}"))
    print(dim("  Ask in Thai or English · type /help for commands"))
    print(dim("  " + "─" * 44))
    print()

def print_answer(text: str):
    """Print the assistant's answer with word-wrap."""
    width = min(os.get_terminal_size().columns - 4, 80) if _USE_COLOR else 76
    print()
    for line in text.splitlines():
        if line.strip():
            for wrapped in textwrap.wrap(line, width):
                print(f"  {green(wrapped)}")
        else:
            print()
    print()

def print_debug(cypher: str, results: list[dict]):
    print(dim("  ┌─ Cypher ─────────────────────────────────────"))
    for line in cypher.splitlines():
        print(dim(f"  │ ") + yellow(line))
    print(dim("  ├─ Results ────────────────────────────────────"))
    raw = json.dumps(results, ensure_ascii=False, indent=2)
    for line in raw.splitlines():
        print(dim("  │ ") + dim(line))
    print(dim("  └───────────────────────────────────────────────"))
    print()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Initialise LLM client (validates API key early)
    try:
        llm_manager._get_client()
    except EnvironmentError as e:
        print(red(str(e)))
        sys.exit(1)

    # Connect to Neo4j
    print(dim("Connecting to Neo4j..."), end="\r")
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
    except Exception as e:
        print(red(f"Cannot connect to Neo4j at {NEO4J_URI}"))
        print(red(f"  {e}"))
        sys.exit(1)

    clear_screen()
    print_banner()

    debug_mode = False

    try:
        while True:
            try:
                user_input = input(f"  {cyan('You')}: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n  {dim('Goodbye!')}\n")
                break

            if not user_input:
                continue

            if user_input.lower() in ("/quit", "/exit", "quit", "exit"):
                print(f"\n  {dim('Goodbye!')}\n")
                break
            elif user_input.lower() == "/debug":
                debug_mode = not debug_mode
                state = green("ON") if debug_mode else dim("OFF")
                print(f"  {dim('Debug mode:')} {state}\n")
                continue
            elif user_input.lower().startswith("/check"):
                parts = user_input.split(maxsplit=1)
                keyword = parts[1].strip().lower() if len(parts) > 1 else ""
                try:
                    rows = run_cypher(
                        driver,
                        "MATCH (s:Station) RETURN s.station_id AS id, s.name_en AS en, s.name_th AS th ORDER BY s.line_id, s.sequence_no"
                    )
                    matches = [r for r in rows if not keyword or keyword in r["id"].lower() or keyword in (r["en"] or "").lower()]
                    if matches:
                        print()
                        for r in matches:
                            print(f"  {yellow(r['id'])}  {dim(r['en'])}  {dim(r['th'] or '')}")
                        print()
                    else:
                        print(f"  {dim(f'No stations matching \"{keyword}\"')}\n")
                except Exception as e:
                    print(f"  {red('Check failed:')} {e}\n")
                continue
            elif user_input.lower().startswith("/plan"):
                # Usage:
                #   /plan MRT_SAMYAN
                #   /plan MRT_SAMYAN 3
                #   /plan MRT_SAMYAN 2 Hotel
                #   /plan MRT_SAMYAN 2 Department Store
                parts = user_input.split(maxsplit=3)
                if len(parts) < 2:
                    print(f"  {dim('Usage: /plan <station_id> [hops] [category]')}")
                    print(f"  {dim('Example: /plan MRT_SAMYAN 2 Hotel')}\n")
                    continue

                plan_station = parts[1].strip()
                plan_hops    = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 2
                plan_cat     = parts[3].strip() if len(parts) > 3 else None

                # If arg 2 isn't a digit, treat it as the start of the category
                if len(parts) > 2 and not parts[2].isdigit():
                    plan_hops = 2
                    plan_cat  = " ".join(parts[2:]).strip()

                print(dim(f"  Planning trip from {plan_station} ({plan_hops} hops)..."), end="\r", flush=True)
                try:
                    plan_text = trip_planner.plan_trip(
                        driver,
                        plan_station,
                        hops=plan_hops,
                        category=plan_cat,
                    )
                    print(" " * 50, end="\r")
                    print(f"  {bold('Trip Plan')}:", end="")
                    print_answer(plan_text)
                except RuntimeError as e:
                    print(" " * 50, end="\r")
                    print(f"  {red('Plan failed:')} {e}\n")
                except Exception as e:
                    print(" " * 50, end="\r")
                    print(f"  {red('Unexpected error:')} {e}\n")
                continue
            elif user_input.lower() == "/clear":
                clear_screen()
                print_banner()
                continue
            elif user_input.lower() == "/help":
                print(HELP_TEXT)
                continue
            elif user_input.startswith("/"):
                print(f"  {dim('Unknown command. Type /help for options.')}\n")
                continue

            result = ask(user_input, driver)

            print(" " * 30, end="\r")

            if debug_mode or (result["error"] is None and len(result["results"]) == 0):
                print_debug(result["cypher"], result["results"])

            if result["error"]:
                print(f"  {red('Note:')} {dim(result['error'])}")

            print(f"  {bold('Assistant')}:", end="")
            print_answer(result["answer"])

    finally:
        driver.close()


if __name__ == "__main__":
    main()