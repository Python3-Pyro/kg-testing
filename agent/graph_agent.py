"""Natural-language-to-Cypher agent over the retail knowledge graph in Neo4j.

Uses a tool-use loop with a single `run_cypher` tool, against whichever LLM
provider/model is selected (see agent/providers.py). Unlike Postgres, Neo4j
Community Edition has no RBAC/privilege system to hand the agent a genuinely
restricted credential, so safety rests on two other layers: a keyword/clause
guard (same pattern as SQLAgent), and executing every query through the
driver's read-access-mode transaction. That access-mode check is enforced by
the Cypher engine itself for any single instance (Community or Enterprise) —
a write clause that slips past the keyword guard still gets rejected with an
AccessMode error.

Conversation history is in the selected provider's own wire format, not
portable across providers — see the same note in sql_agent.py.
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase
from neo4j.graph import Node, Relationship

from agent.base import AgentResult
from agent.providers import DEFAULT_MODEL_KEY, get_provider

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MAX_ROWS = 500
MAX_TOOL_ITERATIONS = 6

WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|CALL)\b",
    re.IGNORECASE,
)

GRAPH_SCHEMA_DESCRIPTION = """
You are a graph analyst for a retail company's Neo4j knowledge graph, modeling
the same data as a classic star schema, but as a property graph — enriched
with structural business-rule knowledge that a plain relational schema
doesn't carry. Nodes and relationships:

(:Customer {customer_sk, customer_id, first_name, last_name, email, residential_location, customer_segment, display_name})
(:Product {product_sk, product_id, product_name, category, brand, origin_location})
(:Store {store_sk, store_id, store_name, store_type, store_location})
(:Salesperson {salesperson_sk, salesperson_id, salesperson_name, salesperson_role, display_name})
(:Campaign {campaign_sk, campaign_id, campaign_name, campaign_budget})
(:Date {date_sk, full_date, year, month, day, weekday, quarter})
(:Sale {sales_id, sales_date, total_amount, within_campaign_window})

(:Sale)-[:PURCHASED_BY]->(:Customer)
(:Sale)-[:FOR_PRODUCT]->(:Product)
(:Sale)-[:AT_STORE]->(:Store)
(:Sale)-[:HANDLED_BY]->(:Salesperson)
(:Sale)-[:PART_OF_CAMPAIGN]->(:Campaign)
(:Store)-[:MANAGED_BY]->(:Salesperson)
(:Campaign)-[:STARTS_ON]->(:Date)
(:Campaign)-[:ENDS_ON]->(:Date)

Reference-vocabulary nodes (canonical, whitespace-cleaned — prefer these over
the raw string property when filtering/enumerating, since the raw property
can carry data-entry artifacts like stray whitespace that these don't):
(:StoreType {name})          (:Store)-[:HAS_TYPE]->(:StoreType)
(:CustomerSegment {name})    (:Customer)-[:IN_SEGMENT]->(:CustomerSegment)
(:ProductCategory {name})    (:Product)-[:IN_CATEGORY]->(:ProductCategory)
(:SalespersonRole {name})    (:Salesperson)-[:HAS_ROLE]->(:SalespersonRole)

Knowledge-rule nodes — query these when a question touches campaign
attribution, individual rankings, or categorical filters, and before
concluding a question is unanswerable or needs a heavy scan:
(:BusinessRule {id, title, description, applies_to})
e.g. `MATCH (r:BusinessRule) RETURN r.title, r.description`

Notes:
- Sale.sales_date is a full datetime property, NOT connected to :Date nodes
  (there is no relationship from :Sale to :Date). Use date/datetime functions
  on the sales_date property directly (e.g. date(s.sales_date), s.sales_date.year)
  for time-based questions.
- Sale.within_campaign_window (boolean) is precomputed: whether that sale's
  date actually falls within its linked campaign's advertised start/end
  dates. Campaign attribution itself does NOT require this to be true — see
  the campaign_attribution_not_date_bounded BusinessRule node. Only use this
  property when a question specifically asks about the campaign's advertised
  time period, not for general "sales attributed to campaign X" questions.
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
  keys (see the names_not_unique_disambiguate BusinessRule node). In Cypher,
  `RETURN x.name, sum(y)` implicitly groups by every non-aggregated expression
  returned — if you return just the raw name, you will silently merge
  different people's sales together. When ranking/grouping individuals (e.g.
  "top salesperson", "which customer spent the most"), either include the
  surrogate key alongside the name, or return the display_name property
  directly (it already embeds the natural id, e.g. "Michael Davis (SP00090)",
  so it's safe to group by on its own).
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
    def __init__(self, model_key: str = DEFAULT_MODEL_KEY):
        self.model_key = model_key
        self._provider = get_provider(model_key)
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
            response = self._provider.call(GRAPH_SCHEMA_DESCRIPTION, TOOLS, messages)
            result.input_tokens += response.input_tokens
            result.output_tokens += response.output_tokens

            if response.stop_reason != "tool_use":
                result.answer = response.text
                self._provider.append_assistant_turn(messages, response)
                return result, messages

            self._provider.append_assistant_turn(messages, response)
            tool_results = []
            for tc in response.tool_calls:
                if tc["name"] == "run_cypher":
                    query = tc["input"]["query"]
                    result.queries.append(query)
                    tool_output = self.run_cypher(query)
                    if "error" not in tool_output:
                        result.last_columns = tool_output["columns"]
                        result.last_rows = tool_output["rows"]
                        result.last_truncated = tool_output["truncated"]
                    tool_results.append((tc["id"], tool_output))
            self._provider.append_tool_results(messages, tool_results)

        result.answer = "I couldn't reach a final answer within the tool-call budget."
        return result, messages
