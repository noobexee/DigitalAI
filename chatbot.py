"""
Commands during chat
  /debug    toggle showing generated Cypher + raw results
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
from neo4j import GraphDatabase
from neo4j.exceptions import CypherSyntaxError, ServiceUnavailable

load_dotenv(Path(__file__).parent / ".env")


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
  RETURN [n IN nodes(path) | n.name_en] AS stations, length(path) AS stops

  -- Shortest path between two stations
  MATCH (a:Station {station_id: 'BTS_MO CHIT'}), (b:Station {station_id: 'BTS_ASOK'})
  MATCH path = shortestPath((a)-[:NEXT_TO|CONNECTS_TO*]-(b))
  RETURN [n IN nodes(path) | n.name_en] AS stations, length(path) AS stops

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

def run_cypher(driver, query: str) -> list[dict]:
    with driver.session() as session:
        return [dict(r) for r in session.run(query)]

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
"""

_ANSWER_SYSTEM = SCHEMA_PROMPT + """
Your task: answer the user's question using the provided query results.

General rules:
- Same language as the user (Thai or English).
- Be friendly and clear.
- No mention of Cypher, Neo4j, or technical terms.
- If results are empty, say so and suggest rephrasing.

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
    4. Take the BTS Silom Line to BTS Siam (1 stop)
    Total: 2 stops, 1 interchange

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


def format_answer(question: str, cypher: str, results: list[dict]) -> str:
    """Turn raw Neo4j results into a natural language answer."""
    body = (
        f"User question: {question}\n\n"
        f"Cypher used:\n{cypher}\n\n"
        f"Results:\n{json.dumps(results, ensure_ascii=False, indent=2)}\n\n"
        "Please answer the user's question."
    )
    return llm_manager.call(_ANSWER_SYSTEM, body)


# ---------------------------------------------------------------------------
# Single question → answer
# ---------------------------------------------------------------------------

def ask(question: str, driver) -> dict:
    """Run one question through the full pipeline. Returns result dict."""

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

    # 4. Format answer
    print(dim("  Composing answer..."), end="\r", flush=True)
    try:
        answer = format_answer(question, cypher, results)
    except RuntimeError as e:
        return {"answer": str(e), "cypher": cypher, "results": results, "error": str(e)}

    return {"answer": answer, "cypher": cypher, "results": results, "error": None}

# ---------------------------------------------------------------------------
# Terminal REPL
# ---------------------------------------------------------------------------

HELP_TEXT = f"""
{bold("Available commands")}
  {cyan("/debug")}         — toggle showing generated Cypher and raw results
  {cyan("/check")}         — show all station IDs currently in the graph
  {cyan("/check <word>")}  — search station IDs containing a word  e.g. /check siam
  {cyan("/clear")}         — clear the screen
  {cyan("/help")}          — show this message
  {cyan("/quit")}          — exit the chatbot

{bold("Example questions")}
  What hotels are near BTS Asok?
  How do I get from MRT Samyan to BTS Siam?
  Tourist attractions near MRT Sam Yot
  Which stations connect MRT and BTS?
  โรงแรมใกล้ BTS อโศกมีอะไรบ้าง
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