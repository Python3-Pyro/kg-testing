# kg-testing — Retail SQL NLQ Agent

## Purpose
A natural-language-to-SQL agent over a retail star-schema dataset, exposed through a Streamlit chat UI. Users ask business questions in plain English; the agent writes and runs SQL against Postgres and returns results/explanations.

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
- **Names in this dataset are not unique** — e.g. 3 different salespersons are named "Michael Davis" with different `salesperson_sk` values. The first eval run caught the agent grouping `GROUP BY salesperson_name` for a "top 5 salespersons" question, silently merging distinct people's sales into one inflated, misattributed total (100% recall dropped to 40% on that question). Fixed via an explicit system-prompt rule in `SCHEMA_DESCRIPTION`: always group by the surrogate key alongside the display name when ranking individuals. Worth remembering if similar "top N entity" questions start failing again — check whether a GROUP BY is keying on a non-unique display column.
