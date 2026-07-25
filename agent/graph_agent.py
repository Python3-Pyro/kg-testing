"""Natural-language-to-Cypher agent over the retail knowledge graph in Neo4j.

Uses a Claude tool-use loop with a single `run_cypher` tool. Unlike Postgres,
Neo4j Community Edition has no RBAC/privilege system to hand the agent a
genuinely restricted credential, so safety rests on two other layers: a
keyword/clause guard (same pattern as SQLAgent), and executing every query
through the driver's read-access-mode transaction. That access-mode check is
enforced by the Cypher engine itself for any single instance (Community or
Enterprise) — a write clause that slips past the keyword guard still gets
rejected with an AccessMode error.
"""
import os
import re
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j.graph import Node, Relationship

from agent.base import AgentResult

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
MAX_ROWS = 500
MAX_TOOL_ITERATIONS = 6

WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|CALL)\b",
    re.IGNORECASE,
)

GRAPH_SCHEMA_DESCRIPTION = """
You are a graph analyst for a retail company's Neo4j knowledge graph, modeling
the same data as a classic star schema, but as a property graph. Nodes and
relationships:

(:Customer {customer_sk, customer_id, first_name, last_name, email, residential_location, customer_segment})
(:Product {product_sk, product_id, product_name, category, brand, origin_location})
(:Store {store_sk, store_id, store_name, store_type, store_location})
(:Salesperson {salesperson_sk, salesperson_id, salesperson_name, salesperson_role})
(:Campaign {campaign_sk, campaign_id, campaign_name, campaign_budget})
(:Date {date_sk, full_date, year, month, day, weekday, quarter})
(:Sale {sales_id, sales_date, total_amount})

(:Sale)-[:PURCHASED_BY]->(:Customer)
(:Sale)-[:FOR_PRODUCT]->(:Product)
(:Sale)-[:AT_STORE]->(:Store)
(:Sale)-[:HANDLED_BY]->(:Salesperson)
(:Sale)-[:PART_OF_CAMPAIGN]->(:Campaign)
(:Store)-[:MANAGED_BY]->(:Salesperson)
(:Campaign)-[:STARTS_ON]->(:Date)
(:Campaign)-[:ENDS_ON]->(:Date)

Notes:
- Sale.sales_date is a full datetime property, NOT connected to :Date nodes
  (there is no relationship from :Sale to :Date). Use date/datetime functions
  on the sales_date property directly (e.g. date(s.sales_date), s.sales_date.year)
  for time-based questions.
- Always use the run_cypher tool to execute queries; never guess at data values.
- RETURN specific properties (e.g. c.customer_segment, sum(s.total_amount)),
  not whole nodes (e.g. avoid `RETURN c`) — this keeps results as clean
  scalar values instead of opaque node objects.
- Only read (MATCH/OPTIONAL MATCH/WITH/RETURN/WHERE/ORDER BY/UNWIND-on-literals)
  queries are permitted — no CREATE, MERGE, DELETE, SET, REMOVE, DROP, LOAD CSV,
  or CALL. Add LIMIT when returning raw (non-aggregated) rows.
- After getting results, give a concise natural-language answer citing the
  actual numbers. Do not fabricate results if a query errors — report the
  error and try a corrected query instead.
- Names (salesperson_name, first_name/last_name, etc.) are NOT unique — the
  same name can belong to multiple different people with different surrogate
  keys. In Cypher, `RETURN x.name, sum(y)` implicitly groups by every
  non-aggregated expression returned — if you return just the name, you will
  silently merge different people's sales together. When ranking/grouping
  individuals (e.g. "top salesperson", "which customer spent the most"),
  always include the surrogate key (salesperson_sk, customer_sk, ...) in what
  you RETURN alongside the name, never the name alone.
""".strip()

def _to_native(value):
    """Neo4j Node/Relationship/temporal values aren't JSON-safe scalars on
    their own; unwrap them so eval/UI code sees plain dicts/dates/numbers
    regardless of whether the agent's Cypher returned a whole node or a
    specific property."""
    if isinstance(value, (Node, Relationship)):
        return dict(value)
    if hasattr(value, "to_native"):
        return value.to_native()
    return value


TOOLS = [
    {
        "name": "run_cypher",
        "description": (
            "Execute a read-only Cypher query against the retail knowledge "
            "graph in Neo4j and return the resulting rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A single read-only Cypher query.",
                }
            },
            "required": ["query"],
        },
    }
]


class GraphAgent:
    def __init__(self):
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._driver = GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
        )
        self._driver.verify_connectivity()

    def close(self):
        self._driver.close()

    def run_cypher(self, query: str) -> dict:
        """Execute a read-only Cypher query. Public so eval scripts can reuse
        the same guarded execution path."""
        stripped = query.strip().rstrip(";")
        if WRITE_KEYWORDS.search(stripped):
            return {"error": "Query contains a disallowed write/procedure-call keyword."}

        try:
            def _run(tx):
                res = tx.run(stripped)
                rows = []
                for i, record in enumerate(res):
                    if i >= MAX_ROWS:
                        break
                    rows.append({k: _to_native(v) for k, v in record.items()})
                keys = res.keys()
                return keys, rows

            with self._driver.session() as session:
                keys, rows = session.execute_read(_run)

            truncated = len(rows) == MAX_ROWS
            return {
                "columns": list(keys),
                "rows": rows,
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
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=GRAPH_SCHEMA_DESCRIPTION,
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                text_parts = [b.text for b in response.content if b.type == "text"]
                result.answer = "\n".join(text_parts).strip()
                messages.append({"role": "assistant", "content": response.content})
                return result, messages

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name == "run_cypher":
                    query = block.input["query"]
                    result.queries.append(query)
                    tool_output = self.run_cypher(query)
                    if "error" not in tool_output:
                        result.last_columns = tool_output["columns"]
                        result.last_rows = tool_output["rows"]
                        result.last_truncated = tool_output["truncated"]
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(tool_output),
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        result.answer = "I couldn't reach a final answer within the tool-call budget."
        return result, messages
