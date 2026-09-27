import os
import sys
import logging
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError

load_dotenv(Path(__file__).parent / ".env")

# Config
NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "12345678")

CSV_DIR = os.path.dirname(os.path.abspath(__file__))   # same folder as this script

PLACE_CSVS = {
    "MRT_BLUE":       "data/mrt_blue_places.csv",
    "BTS_SUKHUMVIT":  "data/bts_sukhumvit_places.csv",
    "BTS_SILOM":      "data/bts_silom_places.csv",
}

STATION_MAP_CSV = "data/station_map.csv"
INTERCHANGE_CSV = "data/interchange.csv"   # cross-line physical interchanges (e.g. MRT_SUKHUMVIT ↔ BTS_ASOK)

# One inline-connection CSV per line.
# Each file has two columns: STATION1_ID, STATION2_ID
# These are non-sequential connections within the same line
# (e.g. a loop branch, an express skip, or a planned shortcut).
INLINE_CONNECTION_CSVS = {
    "MRT_BLUE":      "data/mrt_blue_connections.csv",
    "BTS_SUKHUMVIT": "data/bts_sukhumvit_connections.csv",
    "BTS_SILOM":     "data/bts_silom_connections.csv",
}

LINES = [
    {"line_id": "MRT_BLUE",      "name": "MRT Blue Line",      "color": "#1A4FA0"},
    {"line_id": "BTS_SUKHUMVIT", "name": "BTS Sukhumvit Line", "color": "#009A44"},
    {"line_id": "BTS_SILOM",     "name": "BTS Silom Line",     "color": "#78BE20"},
]

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
logging.getLogger("neo4j").setLevel(logging.WARNING)

# Helpers
def csv_path(filename: str) -> str:
    return os.path.join(CSV_DIR, filename)


def load_csv(filename: str, **kwargs) -> pd.DataFrame:
    path = csv_path(filename)
    if not os.path.exists(path):
        log.error("CSV not found: %s", path)
        sys.exit(1)
    df = pd.read_csv(path, encoding="utf-8-sig", **kwargs)
    log.info("Loaded %-35s  (%d rows)", filename, len(df))
    return df


def run(session, query: str, **params):
    """Execute a single Cypher statement."""
    session.run(query, **params)

#Create Index
SCHEMA_QUERIES = [
    # Uniqueness constraints (also create indexes automatically)
    "CREATE CONSTRAINT station_id IF NOT EXISTS FOR (s:Station) REQUIRE s.station_id IS UNIQUE",
    "CREATE CONSTRAINT line_id    IF NOT EXISTS FOR (l:Line)    REQUIRE l.line_id    IS UNIQUE",
    "CREATE CONSTRAINT place_id   IF NOT EXISTS FOR (p:Place)   REQUIRE p.place_id   IS UNIQUE",
    "CREATE CONSTRAINT category_name IF NOT EXISTS FOR (c:Category) REQUIRE c.name IS UNIQUE",
    # Extra lookup indexes
    "CREATE INDEX station_name_en IF NOT EXISTS FOR (s:Station) ON (s.name_en)",
    "CREATE INDEX station_name_th IF NOT EXISTS FOR (s:Station) ON (s.name_th)",
    "CREATE INDEX place_category   IF NOT EXISTS FOR (p:Place)   ON (p.category)",
]


def create_schema(session):
    log.info("--- Creating schema (constraints + indexes) ---")
    for q in SCHEMA_QUERIES:
        try:
            session.run(q)
        except Exception as e:
            # Older Neo4j versions use different syntax; log and continue
            log.warning("Schema query skipped (%s): %s", type(e).__name__, q[:60])
    log.info("Schema OK")


CREATE_LINE = """
MERGE (l:Line {line_id: $line_id})
SET l.name  = $name,
    l.color = $color
"""


def create_lines(session):
    log.info("--- Creating Line nodes ---")
    for line in LINES:
        run(session, CREATE_LINE, **line)
        log.info("  Line: %s", line["line_id"])


# station_map column mapping:
#   A=col0  B=col1  C=col2   D=col3  E=col4  F=col5   G=col6  H=col7  I=col8
STATION_MAP_COLS = {
    "MRT_BLUE":      (0, 1, 2),   # (ID col, EN name col, TH name col)
    "BTS_SUKHUMVIT": (3, 4, 5),
    "BTS_SILOM":     (6, 7, 8),
}

#Create Station
CREATE_STATION = """
MERGE (s:Station {station_id: $station_id})
SET s.name_en     = $name_en,
    s.name_th     = $name_th,
    s.line_id     = $line_id,
    s.sequence_no = $sequence_no
WITH s
MATCH (l:Line {line_id: $line_id})
MERGE (l)-[:HAS_STATION]->(s)
"""

def create_stations(session):
    log.info("--- Creating Station nodes ---")
    df = load_csv(STATION_MAP_CSV, header=0)   # first row is header

    for line_id, (id_col, en_col, th_col) in STATION_MAP_COLS.items():
        # Extract the three columns for this line, drop NaN rows
        sub = df.iloc[:, [id_col, en_col, th_col]].dropna(subset=[df.columns[id_col]])
        sub.columns = ["station_id", "name_en", "name_th"]
        sub = sub[sub["station_id"].astype(str).str.strip() != ""]

        count = 0
        for seq, row in sub.iterrows():
            run(
                session,
                CREATE_STATION,
                station_id=str(row["station_id"]).strip(),
                name_en=str(row["name_en"]).strip(),
                name_th=str(row["name_th"]).strip() if pd.notna(row["name_th"]) else "",
                line_id=line_id,
                sequence_no=int(seq),
            )
            count += 1
        log.info("  %-20s  %d stations", line_id, count)

# NEXT_TO relationships (sequential stations per line)
CREATE_NEXT_TO = """
MATCH (l:Line {line_id: $line_id})-[:HAS_STATION]->(s:Station)
WITH s ORDER BY s.sequence_no
WITH collect(s) AS stations
UNWIND range(0, size(stations) - 2) AS i
WITH stations[i] AS a, stations[i+1] AS b
MERGE (a)-[:NEXT_TO]->(b)
MERGE (b)-[:NEXT_TO]->(a)
"""


def create_next_to(session):
    log.info("--- Creating NEXT_TO relationships ---")
    for line in LINES:
        run(session, CREATE_NEXT_TO, line_id=line["line_id"])
        log.info("  Linked stations on %s", line["line_id"])

#Place nodes + NEAR / BELONGS_TO relationships

CREATE_PLACE = """
MATCH (s:Station {station_id: $station_id})
MERGE (p:Place {place_id: $place_id})
SET p.name       = $name,
    p.name_th    = $name_th,
    p.category   = $category,
    p.distance_m = $distance_m
MERGE (c:Category {name: $category})
MERGE (s)-[:NEAR {distance_m: $distance_m}]->(p)
MERGE (p)-[:BELONGS_TO]->(c)
"""


def _make_place_id(station_id: str, place_name: str) -> str:
    """Stable unique ID for a Place node."""
    clean = place_name.strip().lower().replace(" ", "_")
    return f"{station_id}__{clean}"


def create_places(session):
    log.info("--- Creating Place nodes + NEAR relationships ---")
    for line_id, filename in PLACE_CSVS.items():
        df = load_csv(filename)

        # Normalize column names:
        #   strip whitespace, replace inner spaces with _, strip chars after first non-word char
        #   e.g. "Place Name "    → "Place_Name"
        #        "Place Name(TH)" → "Place_Name"   ← then disambiguated by position check below
        #        "Distance(m)"    → "Distance"
        def normalize_col(c: str) -> str:
            import re
            c = c.strip()
            c = re.sub(r'\s+', '_', c)          # spaces → underscore
            c = re.sub(r'[^\w].*$', '', c)      # strip anything after first non-word char
            return c

        df.columns = [normalize_col(c) for c in df.columns]

        # "Place Name(TH)" normalizes to the same "Place_Name" as "Place Name".
        # Deduplicate by renaming the second occurrence to "Place_Name_TH".
        seen: dict[str, int] = {}
        new_cols = []
        for col in df.columns:
            if col in seen:
                seen[col] += 1
                new_cols.append(f"{col}_TH")
            else:
                seen[col] = 0
                new_cols.append(col)
        df.columns = new_cols
        log.info("  Normalized columns: %s", list(df.columns))

        required = {"Station_ID", "Place_Name", "Category", "Distance"}
        missing = required - set(df.columns)
        if missing:
            log.error(
                "  %s is missing columns: %s\n"
                "  Columns found after normalization: %s\n"
                "  Raw CSV headers before normalization: re-check your file",
                filename, missing, list(df.columns),
            )
            sys.exit(1)

        df = df.dropna(subset=["Station_ID", "Place_Name"])
        df["Distance"] = pd.to_numeric(df["Distance"], errors="coerce").fillna(0).astype(int)

        count = 0
        for _, row in df.iterrows():
            station_id = str(row["Station_ID"]).strip()
            place_name = str(row["Place_Name"]).strip()

            # name_th is optional — use empty string if column absent or blank
            name_th = ""
            if "Place_Name_TH" in df.columns and pd.notna(row["Place_Name_TH"]):
                name_th = str(row["Place_Name_TH"]).strip()

            run(
                session,
                CREATE_PLACE,
                station_id=station_id,
                place_id=_make_place_id(station_id, place_name),
                name=place_name,
                name_th=name_th,
                category=str(row["Category"]).strip(),
                distance_m=int(row["Distance"]),
            )
            count += 1
        log.info("  %-20s  %d places ingested", line_id, count)


#CONNECTS_TO relationships (cross-line physical interchanges)
# interchange.csv format:
#   Station1_ID,Station2_ID
#   MRT_SUKHUMVIT,BTS_ASOK
#   MRT_SILOM,BTS_SALA_DAENG
CREATE_INTERCHANGE = """
MATCH (s1:Station {station_id: $id1})
MATCH (s2:Station {station_id: $id2})
MERGE (s1)-[:CONNECTS_TO]->(s2)
MERGE (s2)-[:CONNECTS_TO]->(s1)
"""


def create_interchanges(session):
    log.info("--- Creating CONNECTS_TO (cross-line interchange) relationships ---")
    path = csv_path(INTERCHANGE_CSV)
    if not os.path.exists(path):
        log.warning("  interchange.csv not found (skipping): %s", path)
        return

    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]

    required = {"Station1_ID", "Station2_ID"}
    missing = required - set(df.columns)
    if missing:
        log.error("  interchange.csv is missing columns: %s  (expected Station1_ID, Station2_ID)", missing)
        return

    df = df.dropna(subset=["Station1_ID", "Station2_ID"])
    count = 0
    skipped = 0

    for _, row in df.iterrows():
        id1 = str(row["Station1_ID"]).strip()
        id2 = str(row["Station2_ID"]).strip()

        if not id1 or not id2:
            log.warning("  Skipping blank row in interchange.csv")
            skipped += 1
            continue

        # Warn if both IDs belong to the same line (likely a data mistake).
        # Uses full line_id prefix matching so BTS_SUKHUMVIT_SIAM vs BTS_SILOM_SIAM
        # are correctly recognised as different lines.
        known_line_ids = {line["line_id"] for line in LINES}

        def get_line_prefix(station_id: str) -> str | None:
            for lid in known_line_ids:
                if station_id.startswith(lid):
                    return lid
            return None

        line1, line2 = get_line_prefix(id1), get_line_prefix(id2)
        if line1 and line2 and line1 == line2:
            log.warning(
                "  Possible data issue: %s and %s are both on %s — "
                "use the per-line connection CSVs for same-line links",
                id1, id2, line1,
            )

        run(session, CREATE_INTERCHANGE, id1=id1, id2=id2)
        count += 1

    log.info("  %d interchange connections created  (%d skipped)", count, skipped)


# ---------------------------------------------------------------------------
# Step 6 — INLINE_CONNECTION relationships (per-line connection CSVs)
# ---------------------------------------------------------------------------
# These are non-sequential connections within a single line — e.g. a branch,
# an express link, or a loop section.  Relationship used: INLINE_CONNECTION
# (kept separate from CONNECTS_TO which is reserved for cross-line interchanges).
# ---------------------------------------------------------------------------

CREATE_INLINE_CONNECTION = """
MATCH (s1:Station {station_id: $id1})
MATCH (s2:Station {station_id: $id2})
MERGE (s1)-[:INLINE_CONNECTION]->(s2)
MERGE (s2)-[:INLINE_CONNECTION]->(s1)
"""


def _load_connection_csv(filename: str) -> pd.DataFrame | None:
    """
    Load a connection CSV if it exists.
    Returns None (with a warning) if the file is absent so ingestion
    can continue even before all CSVs are ready.
    """
    path = csv_path(filename)
    if not os.path.exists(path):
        log.warning("  Connection CSV not found (skipping): %s", path)
        return None

    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    log.info("Loaded %-40s  (%d rows)", filename, len(df))
    return df


def create_inline_connections(session):
    log.info("--- Creating INLINE_CONNECTION relationships ---")
    total = 0

    for line_id, filename in INLINE_CONNECTION_CSVS.items():
        df = _load_connection_csv(filename)
        if df is None:
            continue

        # Normalize headers the same way as place CSVs:
        # "Station1_ID" → "STATION1_ID", "station2_id" → "STATION2_ID", etc.
        def normalize_conn_col(c: str) -> str:
            import re
            c = c.strip()
            c = re.sub(r'\s+', '_', c)
            c = re.sub(r'[^\w].*$', '', c)
            return c.upper()

        df.columns = [normalize_conn_col(c) for c in df.columns]
        log.info("  Normalized columns: %s", list(df.columns))

        # Validate columns
        required = {"STATION1_ID", "STATION2_ID"}
        missing = required - set(df.columns)
        if missing:
            log.error(
                "  %s is missing columns: %s  (expected STATION1_ID, STATION2_ID)",
                filename, missing,
            )
            continue

        df = df.dropna(subset=["STATION1_ID", "STATION2_ID"])
        count = 0

        for _, row in df.iterrows():
            id1 = str(row["STATION1_ID"]).strip()
            id2 = str(row["STATION2_ID"]).strip()

            if not id1 or not id2:
                log.warning("  Skipping blank row in %s", filename)
                continue

            run(session, CREATE_INLINE_CONNECTION, id1=id1, id2=id2)
            count += 1

        log.info("  %-20s  %d inline connections created", line_id, count)
        total += count

    log.info("  Total inline connections: %d", total)


#Verification summary
VERIFY_QUERIES = [
    ("Line nodes",          "MATCH (l:Line)      RETURN count(l) AS n"),
    ("Station nodes",       "MATCH (s:Station)   RETURN count(s) AS n"),
    ("Place nodes",         "MATCH (p:Place)     RETURN count(p) AS n"),
    ("Category nodes",      "MATCH (c:Category)  RETURN count(c) AS n"),
    ("HAS_STATION rels",    "MATCH ()-[:HAS_STATION]->()  RETURN count(*) AS n"),
    ("NEXT_TO rels",             "MATCH ()-[:NEXT_TO]->()           RETURN count(*) AS n"),
    ("NEAR rels",                "MATCH ()-[:NEAR]->()              RETURN count(*) AS n"),
    ("CONNECTS_TO rels",         "MATCH ()-[:CONNECTS_TO]->()       RETURN count(*) AS n"),
    ("INLINE_CONNECTION rels",   "MATCH ()-[:INLINE_CONNECTION]->() RETURN count(*) AS n"),
]


def verify(session):
    log.info("--- Verification summary ---")
    for label, query in VERIFY_QUERIES:
        result = session.run(query).single()
        log.info("  %-25s %d", label, result["n"])

# Main
def main():
    log.info("Connecting to Neo4j at %s", NEO4J_URI)
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
    except ServiceUnavailable:
        log.error("Cannot reach Neo4j at %s — is it running?", NEO4J_URI)
        sys.exit(1)
    except AuthError:
        log.error("Authentication failed — check NEO4J_USER / NEO4J_PASSWORD")
        sys.exit(1)

    with driver.session() as session:
        create_schema(session)
        create_lines(session)
        create_stations(session)
        create_next_to(session)
        create_places(session)
        create_interchanges(session)
        create_inline_connections(session)
        verify(session)

    driver.close()
    log.info("✓ Ingestion complete")


if __name__ == "__main__":
    main()
