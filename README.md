# Bangkok Transit AI Chatbot

An AI-powered terminal chatbot for Bangkok's BTS and MRT rail network. Ask questions in **Thai or English** about nearby places, routes between stations, and fare estimates — all powered by a Neo4j knowledge graph and a Thai-optimized LLM.

---

## Features

- **Natural language queries** in Thai and English
- **Route finding** with step-by-step interchange instructions
- **Fare estimation** for BTS (Sukhumvit & Silom) and MRT Blue Line
- **Nearby place lookup** by category — hotels, hospitals, department stores, tourist attractions, education centers
- **Switchable LLM backend** — Typhoon (default) or Gemini, changed with one variable
- **Knowledge graph** built on Neo4j with stations, places, categories, and interchange connections

---

## Project Structure

```
DigitalAI/
├── data/                          # CSV data files (see Data section below)
│   ├── station_map.csv
│   ├── interchange.csv
│   ├── mrt_blue_places.csv
│   ├── bts_sukhumvit_places.csv
│   ├── bts_silom_places.csv
│   ├── mrt_blue_connections.csv
│   ├── bts_sukhumvit_connections.csv
│   └── bts_silom_connections.csv
│
├── ingest_graph.py                # Load CSV data into Neo4j
├── chatbot.py                     # Main chatbot — run this
├── trip_planner.py                # Plan visits to nearby places
├── llm_manager.py                  # LLM backend (switch Typhoon ↔ Gemini here)
├── fare.py                        # BTS & MRT fare tables and calculator
├── requirements.txt               # Python dependencies
├── tests/                         # Configuration regression tests
└── .env.example                   # Copy to .env and supply local credentials
```

---

## Requirements

- Python 3.12. The pinned dependencies are verified with this version;
  Python 3.14 currently has a dependency conflict in the Gemini SDK dependency tree.
- [Neo4j Desktop](https://neo4j.com/download/) or Neo4j Community Server (local)
- A Typhoon API key **or** a Google AI Studio API key (see Step 2)

---

## Installation

### 1. Clone and set up the virtual environment

```bash
git clone https://github.com/noobexee/DigitalAI.git
cd DigitalAI

python3.12 -m venv .venv
source .venv/bin/activate

python -m pip install -r requirements.txt
```

On Windows, create the environment with `py -3.12 -m venv .venv` and
activate it with `.venv\Scripts\Activate.ps1` in PowerShell before installing.

Create a new environment on each machine. Virtual environments and Python
cache files are generated locally and excluded from Git. Dependencies are
recorded in `requirements.txt`.

### 2. Get an API key

**Option A — Typhoon (recommended, Thai-optimized, free)**

1. Go to [playground.opentyphoon.ai/api-key](https://playground.opentyphoon.ai/api-key)
2. Sign in and create a free API key

**Option B — Gemini (Google)**

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Sign in and create a free API key

### 3. Create your `.env` file

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Use the key for whichever provider you chose above
TYPHOON_API_KEY=your_typhoon_api_key_here
GOOGLE_API_KEY=your_google_api_key_here    # only needed if using Gemini

# Neo4j connection — use the credentials for your local database
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_neo4j_password_here
```

### 4. Start Neo4j

Open Neo4j Desktop, select your database, and click **Start**. The database must be running before you ingest data or run the chatbot.

### 5. Ingest data into the graph

The repository includes the CSV files in `data/`. Both the ingestion script
and chatbot load `.env` from the project directory; exported environment
variables take precedence. Run:

```bash
python ingest_graph.py
```

You should see a verification summary at the end:

```
Line nodes                3
Station nodes             97
Place nodes               131
Category nodes            5
HAS_STATION rels          99
NEXT_TO rels              192
NEAR rels                 131
CONNECTS_TO rels          10
INLINE_CONNECTION rels    0        ← 0 is fine if connection CSVs aren't ready yet
```

---

## Running the Chatbot

```bash
python chatbot.py
```

The banner shows which model is active:

```
  Bangkok Transit Chatbot
  Model: Typhoon v2.5 30B (SCB 10X)
  Ask in Thai or English · type /help for commands
  ────────────────────────────────────────────────
```

### Example questions

```
What hotels are near BTS Asok?
How do I get from MRT Samyan to BTS Siam?
Tourist attractions near MRT Sam Yot
Hospitals on the MRT Blue Line
Which stations connect MRT and BTS?
โรงแรมใกล้ BTS อโศกมีอะไรบ้าง
ห้างสรรพสินค้าใกล้ MRT สุขุมวิท
```

### Chat commands

| Command | Description |
|---|---|
| `/debug` | Toggle showing the generated Cypher query and raw Neo4j results |
| `/check` | List all station IDs in the graph |
| `/check <word>` | Filter stations by keyword — e.g. `/check siam` |
| `/clear` | Clear the terminal screen |
| `/help` | Show example questions and commands |
| `/quit` | Exit the chatbot |

> **Tip:** Turn on `/debug` if an answer seems wrong — it shows the exact Cypher query the LLM generated so you can identify mismatches.

---

## Switching the LLM

Open `llm_manager.py` and change the `PROVIDER` variable at the top:

```python
# ★  SWITCH PROVIDER HERE  ★
PROVIDER = "typhoon"   # "typhoon" | "gemini"
```

No other changes needed. Make sure the corresponding API key is set in `.env`.

---

## Data Files

All CSV files go in the `data/` folder. Column formats:

### `station_map.csv`

| Column A | Column B | Column C | Column D | Column E | Column F | Column G | Column H | Column I |
|---|---|---|---|---|---|---|---|---|
| MRT Station ID | MRT Name (EN) | MRT Name (TH) | BTS Sukhumvit ID | BTS Sukhumvit Name (EN) | BTS Sukhumvit Name (TH) | BTS Silom ID | BTS Silom Name (EN) | BTS Silom Name (TH) |

### Place CSVs (`mrt_blue_places.csv`, `bts_sukhumvit_places.csv`, `bts_silom_places.csv`)

| Column | Description |
|---|---|
| `Station_ID` | Station ID matching the station_map |
| `Place_Name` | Place name in English |
| `Place_Name(TH)` | Place name in Thai |
| `Category` | One of: `Hotel`, `Department Store`, `Tourist Attractions`, `Education Center`, `Hospital` |
| `Distance(m)` | Walking distance from the station in metres |

### `interchange.csv`

| Column | Description |
|---|---|
| `Station1_ID` | Station on one line |
| `Station2_ID` | Connected station on a different line |

### Connection CSVs (`mrt_blue_connections.csv`, etc.)

| Column | Description |
|---|---|
| `STATION1_ID` | Station on the same line |
| `STATION2_ID` | Connected station (non-sequential, same line) |

> Connection CSVs are optional — the chatbot works fine without them. They are skipped with a warning if the files are missing.

---

## Fare Information

Fares are calculated automatically for route queries. The tables are in `fare.py` and can be updated independently when fares change.

| Line | Minimum | Maximum | Effective |
|---|---|---|---|
| MRT Blue Line | 17 ฿ | 45 ฿ | July 3, 2024 |
| BTS Sukhumvit | 17 ฿ | 65 ฿ | November 1, 2025 |
| BTS Silom | 17 ฿ | 65 ฿ | November 1, 2025 |

To verify the fare tables are correct:

```bash
python fare.py
```

---

## Troubleshooting

**Local checks**

With the virtual environment activated, run:

```bash
python -m pip check
python -m unittest discover -s tests -v
python fare.py
```

These checks do not require Neo4j or LLM credentials. The configuration tests
use temporary fixture credentials. Running ingestion and asking the chatbot
questions additionally requires your database and a configured LLM provider.

**Empty results / wrong station names**
Turn on `/debug` to see the generated Cypher. If the `station_id` looks wrong (e.g. `BTS_SUKHUMVIT_ASOOK` instead of `BTS_ASOK`), use `/check <name>` to find the exact ID and report it — the schema prompt may need updating.

**Rate limit errors**
Typhoon free tier allows 200 requests/min. The chatbot retries automatically up to 3 times. If you hit limits frequently, reduce the number of questions per minute or consider the paid tier.

**Neo4j connection refused**
Make sure the Neo4j database is started in Neo4j Desktop before running any script. Check that `NEO4J_PASSWORD` in `.env` matches your database password.

**`INLINE_CONNECTION rels: 0` after ingestion**
This is normal if you haven't created the connection CSV files yet. The ingestion script skips missing connection files and logs a warning — the rest of the data loads fine.
