"""Natural-language-to-SQL agent over the retail star schema in Postgres.

Uses a tool-use loop with a single `run_sql` tool, against whichever LLM
provider/model is selected (see agent/providers.py — Anthropic, native
OpenAI, or OpenRouter). The DB session is opened read-only at the Postgres
level (defense in depth beyond the keyword guard), so no tool-generated SQL
can mutate data even under prompt injection.

Conversation history (the `messages` list threaded through `ask()`) is in
the selected provider's own wire format, not portable across providers —
don't pass history captured under one model_key into an agent constructed
with a different one.
"""
import os
import re
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from agent.base import AgentResult
from agent.providers import DEFAULT_MODEL_KEY, get_provider

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MAX_ROWS = 500
MAX_TOOL_ITERATIONS = 6

WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|MERGE)\b",
    re.IGNORECASE,
)

SCHEMA_DESCRIPTION = """
You are a SQL analyst for a retail company's Postgres data warehouse, in a
classic star schema. Tables:

dim_dates(date_sk PK, full_date, year, month, day, weekday, quarter)
dim_salespersons(salesperson_sk PK, salesperson_id, salesperson_name, salesperson_role)
dim_customers(customer_sk PK, customer_id, first_name, last_name, email, residential_location, customer_segment)
dim_products(product_sk PK, product_id, product_name, category, brand, origin_location)
dim_stores(store_sk PK, store_id, store_name, store_type, store_location, store_manager_sk FK -> dim_salespersons.salesperson_sk)
dim_campaigns(campaign_sk PK, campaign_id, campaign_name, start_date_sk FK -> dim_dates.date_sk, end_date_sk FK -> dim_dates.date_sk, campaign_budget)
fact_sales(sales_sk PK, sales_id, customer_sk FK, product_sk FK, store_sk FK, salesperson_sk FK, campaign_sk FK, sales_date TIMESTAMP, total_amount NUMERIC)

Notes:
- fact_sales.sales_date is a full timestamp column, NOT joined to dim_dates
  (there is no date_sk FK on fact_sales). Use date/extract functions on
  sales_date directly, or join dim_dates on sales_date::date = full_date if
  calendar attributes (quarter, weekday name, etc.) are needed.
- Always use the run_sql tool to execute queries; never guess at data values.
- Only SELECT queries are permitted. Use table/column aliases and explicit
  JOINs. Add LIMIT when returning raw (non-aggregated) rows.
- After getting results, give a concise natural-language answer citing the
  actual numbers. Do not fabricate results if a query errors — report the
  error and try a corrected query instead.
- Names (salesperson_name, first_name/last_name, etc.) are NOT unique —
  the same name can belong to multiple different people with different
  surrogate keys. When grouping/ranking individuals (e.g. "top salesperson",
  "which customer spent the most"), always GROUP BY the surrogate key
  (salesperson_sk, customer_sk, ...) alongside the name, never by name alone,
  or you will silently merge different people's sales together.
""".strip()

TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Execute a read-only SQL SELECT query against the retail Postgres "
            "database and return the resulting rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A single SQL SELECT statement.",
                }
            },
            "required": ["query"],
        },
    }
]


class SQLAgent:
    def __init__(self, model_key: str = DEFAULT_MODEL_KEY):
        self.model_key = model_key
        self._provider = get_provider(model_key)
        self._conn = psycopg2.connect(
            host=os.environ["POSTGRES_HOST"],
            port=os.environ["POSTGRES_PORT"],
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        )
        # DB-level guard: this session physically cannot commit writes,
        # regardless of what SQL text gets sent to it.
        self._conn.set_session(readonly=True, autocommit=True)

    def close(self):
        self._conn.close()

    def run_sql(self, query: str) -> dict:
        """Execute a read-only SQL query. Public so eval scripts can reuse
        the same guarded execution path to run reference queries."""
        stripped = query.strip().rstrip(";")
        if not re.match(r"^\s*(SELECT|WITH)\b", stripped, re.IGNORECASE):
            return {"error": "Only SELECT statements are allowed."}
        if WRITE_KEYWORDS.search(stripped):
            return {"error": "Query contains a disallowed write/DDL keyword."}

        try:
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(stripped)
                rows = cur.fetchmany(MAX_ROWS + 1)
                truncated = len(rows) > MAX_ROWS
                rows = rows[:MAX_ROWS]
                columns = list(rows[0].keys()) if rows else (
                    [d.name for d in cur.description] if cur.description else []
                )
                return {
                    "columns": columns,
                    "rows": [dict(r) for r in rows],
                    "row_count": len(rows),
                    "truncated": truncated,
                }
        except Exception as e:
            return {"error": str(e)}

    def ask(self, question: str, history: list | None = None) -> AgentResult:
        messages = list(history) if history else []
        messages.append({"role": "user", "content": question})

        result = AgentResult(answer="")

        for _ in range(MAX_TOOL_ITERATIONS):
            response = self._provider.call(SCHEMA_DESCRIPTION, TOOLS, messages)
            result.input_tokens += response.input_tokens
            result.output_tokens += response.output_tokens

            if response.stop_reason != "tool_use":
                result.answer = response.text
                self._provider.append_assistant_turn(messages, response)
                return result, messages

            self._provider.append_assistant_turn(messages, response)
            tool_results = []
            for tc in response.tool_calls:
                if tc["name"] == "run_sql":
                    query = tc["input"]["query"]
                    result.queries.append(query)
                    tool_output = self.run_sql(query)
                    if "error" not in tool_output:
                        result.last_columns = tool_output["columns"]
                        result.last_rows = tool_output["rows"]
                        result.last_truncated = tool_output["truncated"]
                    tool_results.append((tc["id"], tool_output))
            self._provider.append_tool_results(messages, tool_results)

        result.answer = "I couldn't reach a final answer within the tool-call budget."
        return result, messages
