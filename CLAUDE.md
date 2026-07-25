# kg-testing — Retail SQL NLQ Agent

## Purpose
A natural-language-to-SQL agent over a retail star-schema dataset, exposed through a Streamlit chat UI. Users ask business questions in plain English; the agent writes and runs SQL against Postgres and returns results/explanations.

Phase 2 (implemented, see bottom of this file): the same data is also modeled as a Neo4j knowledge graph, queried by a second, graph-only agent (`GraphAgent`), scored against the identical eval set — result: 100% accuracy, matching the SQL agent exactly. Both backends are selectable side by side in the Streamlit app.

See `BUSINESS_RULES.md` for the domain rules/gotchas derived from the ground-truth eval set and empirical data review (attribution semantics, non-unique names, the campaign date-range non-relationship, exact categorical values, etc.) — read that before writing new eval questions or debugging why an agent's answer looks "wrong" but the SQL/Cypher is actually fine.

## Tech stack
- **Database**: Postgres via Docker (`docker-compose.yml`). Docker Desktop is installed but its daemon must be manually started before `docker compose up -d` will work.
- **Agent**: Anthropic SDK with a custom tool-use loop (`agent/sql_agent.py`), no LangChain/LlamaIndex. Single tool, `run_sql`; the full schema is embedded directly in the system prompt (schema is small and fixed, so no `list_tables`/`get_schema` introspection tools were needed).
- **UI**: Streamlit chat app (`app.py`), using `st.chat_input`/`st.chat_message` (requires Streamlit >= 1.38 — the environment originally had 1.21, which predates the chat API, and was upgraded).
- **LLM**: Claude (`claude-sonnet-5` by default, override with `CLAUDE_MODEL` env var), via `ANTHROPIC_API_KEY` in `.env` — configured and verified working.

## Project layout
- `data/` — raw CSVs from Kaggle (gitignored size aside, not loaded automatically; run `db/load_data.py` to load)
- `db/schema.sql` — star-schema DDL (dims + `fact_sales`)
- `db/load_data.py` — creates schema and bulk-loads CSVs into Postgres via `COPY`
- `agent/sql_agent.py` — `SQLAgent` class: opens a **read-only Postgres session** (`conn.set_session(readonly=True)`) plus a keyword guard in `run_sql`, so generated SQL cannot mutate data even under prompt injection. `ask(question, history)` runs the tool-use loop and returns an `AgentResult` (answer text, SQL queries used, last result rows) plus updated message history for multi-turn chat.
- `app.py` — Streamlit chat UI; caches the `SQLAgent` with `st.cache_resource`, keeps both a display history and the raw Anthropic message history in `st.session_state`.
- `docker-compose.yml` — single Postgres 16 service, config pulled from `.env` (`POSTGRES_USER/PASSWORD/DB/HOST/PORT`)
- `requirements.txt` — pinned deps
- `eval/ground_truth.json` — 20 hand-written {question, reference_sql, tags} cases spanning aggregates, joins, rankings, time filters, and categorical filters across the whole schema
- `eval/metrics.py` — `compare_results(expected_rows, actual_rows)`: flattens both result sets to a normalized multiset of scalar values and checks the reference's values are all present in the agent's. Value-based (not row/column-positional) on purpose — the agent is free to name/order/alias columns however it wants, so structural comparison would produce false failures.
- `eval/run_eval.py` — runs every ground-truth question through `SQLAgent.ask`, scores it against the reference SQL's live result (executed via the same `run_sql` read-only path), writes a timestamped JSON to `eval/results/` (gitignored). `run_eval(agent=..., progress_callback=...)` is the reusable entrypoint — CLI (`python -m eval.run_eval`) opens its own agent/connection; the Streamlit Evaluation tab passes in the already-cached one.
- `agent/sql_agent.py`'s `run_sql` method is public (not `_run_sql`) specifically so `eval/run_eval.py` can reuse the exact same guarded execution path to run reference queries.

## Setup / run
1. Start Docker Desktop, then `docker compose up -d`
   - On this machine, `docker` is **not** on PATH — Docker Desktop is installed under `AppData\Local\Programs\DockerDesktop`, not `Program Files`. If `docker` isn't found, or `compose up` fails with `error getting credentials - err: exec: "docker-credential-desktop"`, add `AppData\Local\Programs\DockerDesktop\resources\bin` to PATH for the session first.
2. `pip install -r requirements.txt`
3. `python db/load_data.py` (creates tables, loads all CSVs — only `fact_sales_normalized.csv` is loaded as `fact_sales`; `fact_sales_denormalized.csv` is intentionally not loaded, see below)
4. `streamlit run app.py` — always run from the project root (not `cd app.py`'s dir some other way), since both `agent` and `eval` are imported as top-level packages and need the root on `sys.path`. Running `eval/run_eval.py` directly as a script (`python eval/run_eval.py`) breaks for the same reason — use `python -m eval.run_eval` instead.

## Data
Source: Kaggle dataset `shrinivasv/retail-store-star-schema-dataset`, downloaded with `kagglehub` and moved into `data/`. Raw CSVs — not yet loaded into Postgres.

Star schema: `fact_sales_normalized` is the fact table; `fact_sales_denormalized` is the same fact table with several dimension attributes flattened in (convenient for quick queries, but redundant with the dims — prefer the normalized fact + joins for the agent's schema so it learns real relationships).

| File | Rows | Grain / role |
|---|---|---|
| `dim_customers.csv` | 100,000 | one row per customer |
| `dim_products.csv` | 210 | one row per product |
| `dim_stores.csv` | 500 | one row per store |
| `dim_salespersons.csv` | 2,000 | one row per salesperson |
| `dim_campaigns.csv` | 50 | one row per marketing campaign |
| `dim_dates.csv` | 366 | one row per calendar date (2024) |
| `fact_sales_normalized.csv` | 1,000,000 | one row per sale; FKs only |
| `fact_sales_denormalized.csv` | 1,000,000 | same sales, with dim attributes inlined |

### Columns

**dim_customers**: `customer_sk, customer_id, first_name, last_name, email, residential_location, customer_segment`

**dim_products**: `product_sk, product_id, product_name, category, brand, origin_location`

**dim_stores**: `store_sk, store_id, store_name, store_type, store_location, store_manager_sk` (FK → dim_salespersons.salesperson_sk)

**dim_salespersons**: `salesperson_sk, salesperson_id, salesperson_name, salesperson_role`

**dim_campaigns**: `campaign_sk, campaign_id, campaign_name, start_date_sk, end_date_sk` (FKs → dim_dates.date_sk), `campaign_budget`

**dim_dates**: `full_date, date_sk, year, month, day, weekday, quarter`

**fact_sales_normalized**: `sales_sk, sales_id, customer_sk, product_sk, store_sk, salesperson_sk, campaign_sk, sales_date, total_amount` — all `*_sk` are FKs to the matching dim table's surrogate key; `sales_date` is a full timestamp (not a `date_sk` FK, despite `dim_dates` existing).

**fact_sales_denormalized**: same as above plus `customer_segment, category, store_type, store_location, campaign_name` inlined from dims.

## Conventions
- The agent must only ever execute read-only SQL (`SELECT`/`WITH`) against Postgres. This is enforced twice: a keyword/prefix guard in `SQLAgent.run_sql`, and — the real backstop — the Postgres session itself is opened read-only, so even a successful prompt-injection can't write.
- Only `fact_sales_normalized.csv` is loaded into Postgres (as table `fact_sales`). `fact_sales_denormalized.csv` is left unloaded on purpose: it duplicates dimension columns onto the fact table, which would let the agent skip joins and never learn the real star-schema relationships.
- Tool results from `run_sql` are capped at 500 rows (`MAX_ROWS` in `sql_agent.py`) to keep context/UI usable; the agent is instructed to add its own `LIMIT` for row-level (non-aggregated) queries.
- **Names in this dataset are not unique** — e.g. 3 different salespersons are named "Michael Davis" with different `salesperson_sk` values. The first eval run caught the agent grouping `GROUP BY salesperson_name` for a "top 5 salespersons" question, silently merging distinct people's sales into one inflated, misattributed total (100% recall dropped to 40% on that question). Fixed via an explicit system-prompt rule in `SCHEMA_DESCRIPTION`: always group by the surrogate key alongside the display name when ranking individuals. Worth remembering if similar "top N entity" questions start failing again — check whether a GROUP BY is keying on a non-unique display column. The same trap applies to Cypher (see Phase 2 below) — Cypher's `RETURN x.name, sum(y)` implicitly groups by whatever non-aggregated expressions are returned, so returning just a name re-creates the exact same bug.

---

## Phase 2: Knowledge Graph (Neo4j) — IMPLEMENTED

**Goal**: model the same retail data as a property graph in Neo4j Community Edition (local, via Docker), build a second agent (`GraphAgent`) that answers questions using *only* Cypher against that graph (no SQL/Postgres access), and re-run it against the exact same `eval/ground_truth.json` set to get a directly comparable accuracy number against the SQL agent's current 100%. Decision made with the user: **graph-only agent**, not a hybrid SQL+Cypher agent — a hybrid would let the model fall back to SQL and the eval would trivially stay at 100%, telling us nothing about the graph representation's retrieval quality in isolation.

**Result**: `GraphAgent` scored **100% (20/20)**, identical to the SQL agent's baseline, avg latency 4.9s vs SQL's 3.9s (Cypher traversal is a bit slower than Postgres's indexed joins on this schema/data size, unsurprising and not something either eval run treats as a failure — only `accuracy`/`value_recall` gate pass/fail). Notably, the graph agent got the surrogate-key grouping right (the "Michael Davis" trap, see Conventions above) on its first attempt without needing an iteration-and-fix cycle the way the SQL agent did — the system prompt already carried that lesson forward before `GraphAgent` was ever run. Also handled the quarter-aggregation question (`gt_08`, no `Sale`→`Date` relationship by design) cleanly using Neo4j's native temporal accessor, `date(s.sales_date).quarter`, directly on the property — no join needed at all, notably simpler than the SQL agent's `sales_date::date = full_date` join.

### Graph data model
Star schema facts translate to a graph using the standard "hub node" pattern: since a sale is an n-ary fact (touches 5 dimensions at once — customer, product, store, salesperson, campaign), it becomes its own node with outgoing relationships to each participant, rather than trying to force it into pairwise edges (which would lose whichever dimensions aren't part of the edge).

Nodes carry the dimension's surrogate key (`*_sk`) as the uniqueness constraint — same key the fact CSV already uses as its FK, so the loader can `MATCH`/`MERGE` on it directly without an extra join — plus the natural business id and all descriptive attributes as properties.

```
(:Customer {customer_sk UNIQUE, customer_id, first_name, last_name, email, residential_location, customer_segment})
(:Product  {product_sk UNIQUE, product_id, product_name, category, brand, origin_location})
(:Store    {store_sk UNIQUE, store_id, store_name, store_type, store_location})
(:Salesperson {salesperson_sk UNIQUE, salesperson_id, salesperson_name, salesperson_role})
(:Campaign {campaign_sk UNIQUE, campaign_id, campaign_name, campaign_budget})
(:Date     {date_sk UNIQUE, full_date, year, month, day, weekday, quarter})
(:Sale     {sales_id UNIQUE, sales_date, total_amount})

(:Sale)-[:PURCHASED_BY]->(:Customer)
(:Sale)-[:FOR_PRODUCT]->(:Product)
(:Sale)-[:AT_STORE]->(:Store)
(:Sale)-[:HANDLED_BY]->(:Salesperson)
(:Sale)-[:PART_OF_CAMPAIGN]->(:Campaign)
(:Store)-[:MANAGED_BY]->(:Salesperson)          -- from dim_stores.store_manager_sk
(:Campaign)-[:STARTS_ON]->(:Date)               -- from dim_campaigns.start_date_sk
(:Campaign)-[:ENDS_ON]->(:Date)                 -- from dim_campaigns.end_date_sk
```

`Sale.sales_date` stays a raw property (not a relationship to `:Date`) — mirrors the relational model, where `fact_sales.sales_date` isn't FK'd to `dim_dates` either. Time-based questions filter/extract on the datetime property directly, same as the SQL agent does today.

Only `fact_sales_normalized.csv` feeds `:Sale` nodes, for the same reason it's the only one loaded into Postgres: the denormalized CSV's inlined dimension columns would let the agent skip real graph traversal.

Constraints (uniqueness constraints are supported in Neo4j Community; node-key/property-existence constraints are Enterprise-only, but plain uniqueness is all we need — it also auto-creates the index each MERGE-based load needs to be fast at 1M rows):
```cypher
CREATE CONSTRAINT FOR (c:Customer) REQUIRE c.customer_sk IS UNIQUE;
CREATE CONSTRAINT FOR (p:Product) REQUIRE p.product_sk IS UNIQUE;
CREATE CONSTRAINT FOR (s:Store) REQUIRE s.store_sk IS UNIQUE;
CREATE CONSTRAINT FOR (sp:Salesperson) REQUIRE sp.salesperson_sk IS UNIQUE;
CREATE CONSTRAINT FOR (c:Campaign) REQUIRE c.campaign_sk IS UNIQUE;
CREATE CONSTRAINT FOR (d:Date) REQUIRE d.date_sk IS UNIQUE;
CREATE CONSTRAINT FOR (s:Sale) REQUIRE s.sales_id IS UNIQUE;
```

### Deployment
Add a `neo4j` service to the existing `docker-compose.yml` (image `neo4j:5-community`, explicit tag so it's unambiguous this isn't Enterprise), ports `7474` (browser UI, handy for eyeballing the graph while building it) and `7687` (Bolt, used by the Python driver), credentials via `NEO4J_AUTH=${NEO4J_USER}/${NEO4J_PASSWORD}`, persistent volume for `/data`. New `.env`/`.env.example` vars: `NEO4J_URI` (`bolt://localhost:7687`), `NEO4J_USER`, `NEO4J_PASSWORD`.

**Known Neo4j Community Edition limitations** (checked against Enterprise-only feature list before committing to this plan):
- No RBAC / privilege system — you can create extra users, but every user has full read+write on the database; there's no way to hand the agent a genuinely restricted DB credential the way `POSTGRES` gets a read-only session role.
- Single default database only (no multi-database) — not a problem here, we only need one graph.
- Because there's no RBAC backstop, safety for the `GraphAgent` rests on two layers instead of Postgres's two: (1) the same keyword/clause guard pattern as `SQLAgent.run_sql` (reject `CREATE`, `MERGE`, `DELETE`, `DETACH DELETE`, `SET`, `REMOVE`, `DROP`, `LOAD CSV`, `CALL {apoc,dbms}.*`); (2) executing every query through the driver's **read-access-mode transaction** (`session.execute_read(...)` / `routing_=RoutingControl.READ`). This access-mode check is enforced by the Cypher engine itself for any single instance, Community or Enterprise — it's not just an Enterprise-cluster routing hint — so a write clause slipping past the keyword guard still gets rejected by Neo4j with an `AccessMode` error. That's the real analogue of Postgres's `set_session(readonly=True)` backstop.

### Loading
`graph/load_graph.py`, mirroring the shape of `db/load_data.py`: reads the same `data/*.csv` files (no new download needed), applies `graph/schema.cypher` constraints first, then batch-loads with the official `neo4j` Python driver using `UNWIND $rows AS row MERGE (...)` in chunks of 5,000 rows/transaction rather than one `CREATE` per row. Load order: all 6 dimension node sets first, then `:Sale` nodes + their 5 relationships from `fact_sales_normalized.csv` in one pass (200 batches), then `MANAGED_BY`/`STARTS_ON`/`ENDS_ON`. Actual loaded counts, verified against source row counts and cross-checked against Postgres query results (identical numbers both ways): 100,000 Customer / 210 Product / 500 Store / 2,000 Salesperson / 50 Campaign / 366 Date nodes, 1,000,000 Sale nodes with 5,000,000 outgoing relationships, 500 MANAGED_BY, 50 STARTS_ON, 50 ENDS_ON. Full 1M-row load completes in a few minutes.

### Agent
`agent/graph_agent.py`, structurally parallel to `sql_agent.py`:
- `GraphAgent` class, single tool `run_cypher`, own system prompt (`GRAPH_SCHEMA_DESCRIPTION`) describing the node labels/relationships/properties above — model schema, not SQL DDL.
- Guard posture as documented above (keyword guard + read-access-mode execution via `session.execute_read`) — both independently verified: the keyword guard rejects `CREATE`/`DETACH DELETE` text, and — bypassing the guard on purpose to test the real backstop — a raw write Cypher statement run through `execute_read` gets rejected by Neo4j itself with `Neo.ClientError.Statement.AccessMode`, confirming the access-mode check is enforced at the engine level, not just a cluster-routing hint.
- Carries forward the surrogate-key grouping lesson from the SQL agent's system prompt (see Conventions above), rephrased for Cypher's implicit-GROUP-BY-on-RETURN semantics. It worked — the graph agent grouped by `salesperson_sk` correctly on its first attempt, no fix-and-rerun cycle needed the way the SQL agent required.
- System prompt also tells the agent to `RETURN` specific properties, not whole nodes (avoid `RETURN c`). Defense in depth for this: `GraphAgent.run_cypher` runs every returned value through a `_to_native()` helper that unwraps `neo4j.graph.Node`/`Relationship` objects into plain dicts and converts Neo4j's temporal types (`Date`, `DateTime`, ...) via `.to_native()` into standard Python `date`/`datetime` — needed because raw Neo4j driver objects aren't JSON-serializable scalars, and `eval/metrics.py`'s comparator expects flattenable scalar values, not opaque driver objects.
- Shares `agent.base.AgentResult` with `SQLAgent` (moved out of `sql_agent.py` into `agent/base.py` for this reason) — field renamed `sql_queries` → `queries` since it now holds Cypher too. Both agents are fully interchangeable from the caller's side (eval harness, Streamlit).

### Eval re-run
`eval/run_eval.py` was refactored: `run_eval(oracle=None, agent_under_test=None, backend_label="sql", progress_callback=None)`. `oracle` always runs the reference SQL against Postgres regardless of backend under test (a `GraphAgent` has no way to answer the reference query itself) — if omitted, `run_eval` creates and owns (closes) one itself. `agent_under_test` defaults to `oracle` (reproduces the original SQL-scores-itself behavior); pass a `GraphAgent` to score the graph backend. Callers that pass their own `agent_under_test`/`oracle` are responsible for closing them — `run_eval` only closes what it created by default. `eval/metrics.py`'s `compare_results` needed zero changes — already backend-agnostic. CLI: `python -m eval.run_eval --backend {sql,graph}`. Output filenames are backend-labeled: `eval/results/<timestamp>__sql.json` / `__graph.json`.

**Result** (see Goal section above for the numbers): both backends hit 100%. Re-running the SQL eval after the refactor first (before building `GraphAgent`) confirmed the refactor itself introduced no regression — still 20/20 — before the graph comparison was trusted.

### Streamlit
`app.py` has a `st.radio` backend selector (SQL / Graph) in the Chat tab, with fully separate per-backend chat state (`st.session_state.chats["sql"|"graph"]` — separate display + Anthropic message history, since the two agents have different tools/system prompts and shouldn't share conversation context). Each backend's agent is created lazily via its own `@st.cache_resource` getter and fails gracefully (inline `st.error` with a backend-specific fix hint) without taking down the other backend or the Evaluation tab. The Evaluation tab shows both backends side by side in two columns, each with its own "Run evaluation" button and latest-results metrics, plus a shared "Inspect a question" section with a backend toggle underneath.

### Project layout additions (Phase 2)
- `docker-compose.yml` — `neo4j` service (`neo4j:5-community`, ports 7474/7687, `neo4jdata` volume)
- `.env` / `.env.example` — `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`
- `requirements.txt` — `neo4j` (official Python driver, v5+)
- `graph/schema.cypher` — 7 uniqueness constraints (one per node label)
- `graph/load_graph.py` — batched CSV → graph loader (`python -m graph.load_graph`)
- `agent/base.py` — shared `AgentResult` dataclass (`queries` field, backend-agnostic)
- `agent/graph_agent.py` — `GraphAgent`, Cypher tool-use loop
- `eval/run_eval.py` — refactored for oracle/agent-under-test split, `--backend` CLI flag
- `app.py` — backend selector in Chat tab; side-by-side SQL/Graph scores in Evaluation tab

### Setup / run (Phase 2 additions)
1. `docker compose up -d neo4j` (or `docker compose up -d` for both services) — same PATH caveat as Postgres above
2. `python -m graph.load_graph` (run from project root, needs the root on `sys.path` same as other module-style entrypoints)
3. `python -m eval.run_eval --backend graph` to score the graph agent independently, or use the Streamlit Evaluation tab's Graph column

---

## Phase 3: Business-Rule-Enriched Graph vs. Vanilla SQL — PLANNED, not yet implemented

**Hypothesis**: a knowledge graph that encodes `BUSINESS_RULES.md`'s rules as first-class structure (not just prose the agent has to remember) answers rule-sensitive questions more reliably than a vanilla relational T2SQL agent that only has the raw star schema. The 20-question baseline eval doesn't test this — none of those questions specifically probe a gotcha rule, which is exactly why both backends already hit 100% on it.

**Constraint agreed with the user**: the Postgres schema and `SQLAgent` stay **completely untouched** — a true vanilla baseline. All enrichment goes into the graph (schema + `GraphAgent`'s prompt) only. This is deliberately an unfair comparison in the sense that one side gets enriched and the other doesn't — that asymmetry is the entire point of the experiment.

**Additive-only constraint**: nothing already in the graph (Phase 2's node labels/relationships/properties) gets removed, renamed, or altered. Every Phase 3 change is a pure addition, loaded via new `MERGE`-based functions appended to `graph/load_graph.py` (safe to re-run, same idempotent pattern as the rest of the loader). The existing 20-question eval must still score 20/20 on the graph backend after enrichment — that's the regression check before any new rule-probing questions get trusted.

### Enrichment layer 1 — canonical reference-vocabulary nodes
Replace the four controlled-vocabulary string properties with first-class reference nodes, canonicalized (`.strip()`) once at load time — the raw string property stays on the entity too (additive, existing queries unaffected):

```
(:StoreType {name})        (:Store)-[:HAS_TYPE]->(:StoreType)
(:CustomerSegment {name})  (:Customer)-[:IN_SEGMENT]->(:CustomerSegment)
(:ProductCategory {name})  (:Product)-[:IN_CATEGORY]->(:ProductCategory)
(:SalespersonRole {name})  (:Salesperson)-[:HAS_ROLE]->(:SalespersonRole)
```

Directly fixes the `'Supermarkets '` trailing-space gotcha (`BUSINESS_RULES.md`) at the data layer — canonicalized once at load time, so `MATCH (t:StoreType {name: 'Supermarkets'})` always matches correctly, no `TRIM()` required and no way for an LLM to get it wrong. Also gives the agent a clean, complete enumeration of valid values for free (`MATCH (t:StoreType) RETURN t.name`), impossible to typo or miss a value on.

### Enrichment layer 2 — materialized derived properties
- **`Sale.within_campaign_window: boolean`** — computed at load time from `sales_date` vs. the linked campaign's `STARTS_ON`/`ENDS_ON` dates (already in the graph). Makes the single most non-obvious rule in `BUSINESS_RULES.md` — campaign attribution is FK-only, *not* date-bounded, and only 39.5% of attributed sales actually fall in-window — trivially queryable instead of requiring the agent to reconstruct that join+comparison logic itself, or worse, not realize the distinction exists at all.
- **`Customer.display_name` / `Salesperson.display_name`**: `"{name} ({natural_id})"` (e.g. `"Michael Davis (SP00090)"`) — disambiguates identity structurally, so even a naive `RETURN sp.display_name, sum(...)` (the exact mistake the SQL agent made on `gt_05` before the prompt fix) can no longer silently merge different people. Worth noting honestly: this specific technique isn't graph-exclusive — a Postgres generated column could do the same — it's excluded from Postgres here only because of the vanilla-baseline constraint above, not because SQL is structurally incapable of it.

### Enrichment layer 3 — `:BusinessRule` knowledge nodes
```
(:BusinessRule {id, title, description, applies_to: [labels]})
```
One node per `BUSINESS_RULES.md` rule that doesn't fully reduce to structure even after layers 1–2 — e.g. the *interpretation* that campaign attribution is FK-only (materializing `within_campaign_window` gives the data, but the agent still needs to know not to assume it should filter on it), the "always disambiguate individuals" rule, "every sale has a campaign, there's no organic-sale subset," the campaign-budget-vs-revenue unit mismatch, single-year dataset. This is what makes the enrichment a genuine *knowledge* graph rather than just a property-graph mirror of the star schema: rules become queryable data (`MATCH (r:BusinessRule) RETURN r.title, r.description`) that can grow later by inserting nodes, not by redeploying `GRAPH_SCHEMA_DESCRIPTION` Python code.

`GraphAgent`'s system prompt gets updated to describe the new labels/relationships/properties and to instruct it to consult `:BusinessRule` nodes when a question touches campaigns, categorical filters, or individual rankings.

### Verification plan (before any new proof questions get trusted)
1. Extend `graph/load_graph.py` with loader functions for layers 1–3.
2. Update `GRAPH_SCHEMA_DESCRIPTION` in `agent/graph_agent.py`.
3. Re-run `python -m eval.run_eval --backend graph` on the existing 20 questions — must stay 20/20 (regression check; these questions don't exercise the new rules, so any drop means the enrichment broke something, not that it's "working as intended but eval-incompatible").

### Candidate proof questions (illustrative — user wants to finalize this set as a separate follow-up step, not now)
1. *"How many sales happened at Supermarkets?"* — vanilla SQL's exact-match `store_type = 'Supermarkets'` returns **zero rows** unless the agent happens to think to `TRIM()`/`LIKE`; the graph's `:StoreType` node returns the correct count with no special reasoning required.
2. *"What fraction of [Campaign X]'s attributed revenue came from sales that happened during the campaign's advertised dates?"* — vanilla SQL must reconstruct the `dim_dates` join + date comparison from scratch, and first realize this is a meaningful, distinct question from "revenue attributed to the campaign." The graph reads straight off `Sale.within_campaign_window`.
3. *"There's a salesperson named Michael Davis — how much total revenue has he generated?"* — deliberately ambiguous singular framing (3 distinct Michael Davises exist). Watch whether each backend asks for disambiguation, silently picks one, or incorrectly sums all three.
4. *"List all distinct store types."* — `SELECT DISTINCT store_type` surfaces the ugly `'Supermarkets '` value as-is; the graph's canonical `:StoreType` nodes return it clean.
5. *"Are there any sales not linked to a marketing campaign?"* — tests whether a backend can answer from stated knowledge (a `:BusinessRule` node, or `BUSINESS_RULES.md` context) instead of needing to scan the 1M-row fact table to discover the answer is definitionally zero — an efficiency proof as much as a correctness one.
