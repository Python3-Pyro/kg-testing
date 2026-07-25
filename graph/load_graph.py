"""Loads the star-schema CSVs into Neo4j as a property graph.

Usage: python -m graph.load_graph
Requires the Neo4j container to be up (docker compose up -d neo4j) and .env configured.
"""
import csv
import os
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "graph" / "schema.cypher"
BATCH_SIZE = 5000

DIM_SPECS = [
    ("Customer", "dim_customers.csv", "customer_sk", lambda r: {
        "customer_sk": int(r["customer_sk"]),
        "customer_id": r["customer_id"],
        "first_name": r["first_name"],
        "last_name": r["last_name"],
        "email": r["email"],
        "residential_location": r["residential_location"],
        "customer_segment": r["customer_segment"],
    }),
    ("Product", "dim_products.csv", "product_sk", lambda r: {
        "product_sk": int(r["product_sk"]),
        "product_id": r["product_id"],
        "product_name": r["product_name"],
        "category": r["category"],
        "brand": r["brand"],
        "origin_location": r["origin_location"],
    }),
    ("Store", "dim_stores.csv", "store_sk", lambda r: {
        "store_sk": int(r["store_sk"]),
        "store_id": r["store_id"],
        "store_name": r["store_name"],
        "store_type": r["store_type"],
        "store_location": r["store_location"],
    }),
    ("Salesperson", "dim_salespersons.csv", "salesperson_sk", lambda r: {
        "salesperson_sk": int(r["salesperson_sk"]),
        "salesperson_id": r["salesperson_id"],
        "salesperson_name": r["salesperson_name"],
        "salesperson_role": r["salesperson_role"],
    }),
    ("Campaign", "dim_campaigns.csv", "campaign_sk", lambda r: {
        "campaign_sk": int(r["campaign_sk"]),
        "campaign_id": r["campaign_id"],
        "campaign_name": r["campaign_name"],
        "campaign_budget": float(r["campaign_budget"]),
    }),
    ("Date", "dim_dates.csv", "date_sk", lambda r: {
        "date_sk": int(r["date_sk"]),
        "full_date": date.fromisoformat(r["full_date"]),
        "year": int(r["year"]),
        "month": int(r["month"]),
        "day": int(r["day"]),
        "weekday": int(r["weekday"]),
        "quarter": int(r["quarter"]),
    }),
]

SALE_QUERY = """
UNWIND $rows AS row
MERGE (s:Sale {sales_id: row.sales_id})
SET s.sales_date = row.sales_date, s.total_amount = row.total_amount
WITH s, row
MATCH (c:Customer {customer_sk: row.customer_sk})
MATCH (p:Product {product_sk: row.product_sk})
MATCH (st:Store {store_sk: row.store_sk})
MATCH (sp:Salesperson {salesperson_sk: row.salesperson_sk})
MATCH (ca:Campaign {campaign_sk: row.campaign_sk})
MERGE (s)-[:PURCHASED_BY]->(c)
MERGE (s)-[:FOR_PRODUCT]->(p)
MERGE (s)-[:AT_STORE]->(st)
MERGE (s)-[:HANDLED_BY]->(sp)
MERGE (s)-[:PART_OF_CAMPAIGN]->(ca)
"""


def get_driver():
    return GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )


def read_csv(name):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def batches(rows, size=BATCH_SIZE):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def scalar(driver, query):
    return driver.execute_query(query).records[0][0]


def apply_schema(driver):
    statements = [s.strip() for s in SCHEMA_FILE.read_text().split(";") if s.strip()]
    for stmt in statements:
        driver.execute_query(stmt)
    print("Schema constraints applied.")


def load_dimension_nodes(driver):
    for label, filename, key, transform in DIM_SPECS:
        rows = [transform(r) for r in read_csv(filename)]
        query = f"UNWIND $rows AS row MERGE (n:{label} {{{key}: row.{key}}}) SET n += row"
        for batch in batches(rows):
            driver.execute_query(query, rows=batch)
        count = scalar(driver, f"MATCH (n:{label}) RETURN count(n)")
        print(f"{label}: {count} nodes")


def load_sales(driver):
    rows = read_csv("fact_sales_normalized.csv")
    transformed = [
        {
            "sales_id": r["sales_id"],
            "customer_sk": int(r["customer_sk"]),
            "product_sk": int(r["product_sk"]),
            "store_sk": int(r["store_sk"]),
            "salesperson_sk": int(r["salesperson_sk"]),
            "campaign_sk": int(r["campaign_sk"]),
            "sales_date": datetime.fromisoformat(r["sales_date"]),
            "total_amount": float(r["total_amount"]),
        }
        for r in rows
    ]

    total_batches = -(-len(transformed) // BATCH_SIZE)
    for i, batch in enumerate(batches(transformed), start=1):
        driver.execute_query(SALE_QUERY, rows=batch)
        if i % 10 == 0 or i == total_batches:
            print(f"  sales batch {i}/{total_batches}")

    count = scalar(driver, "MATCH (n:Sale) RETURN count(n)")
    rel_count = scalar(driver, "MATCH (:Sale)-[r]->() RETURN count(r)")
    print(f"Sale: {count} nodes, {rel_count} outgoing relationships")


def load_dim_relationships(driver):
    store_rows = [
        {"store_sk": int(r["store_sk"]), "manager_sk": int(r["store_manager_sk"])}
        for r in read_csv("dim_stores.csv")
    ]
    driver.execute_query(
        """
        UNWIND $rows AS row
        MATCH (st:Store {store_sk: row.store_sk})
        MATCH (sp:Salesperson {salesperson_sk: row.manager_sk})
        MERGE (st)-[:MANAGED_BY]->(sp)
        """,
        rows=store_rows,
    )

    campaign_rows = [
        {
            "campaign_sk": int(r["campaign_sk"]),
            "start_date_sk": int(r["start_date_sk"]),
            "end_date_sk": int(r["end_date_sk"]),
        }
        for r in read_csv("dim_campaigns.csv")
    ]
    driver.execute_query(
        """
        UNWIND $rows AS row
        MATCH (ca:Campaign {campaign_sk: row.campaign_sk})
        MATCH (d1:Date {date_sk: row.start_date_sk})
        MATCH (d2:Date {date_sk: row.end_date_sk})
        MERGE (ca)-[:STARTS_ON]->(d1)
        MERGE (ca)-[:ENDS_ON]->(d2)
        """,
        rows=campaign_rows,
    )
    managed_count = scalar(driver, "MATCH (:Store)-[r:MANAGED_BY]->() RETURN count(r)")
    starts_count = scalar(driver, "MATCH (:Campaign)-[r:STARTS_ON]->() RETURN count(r)")
    ends_count = scalar(driver, "MATCH (:Campaign)-[r:ENDS_ON]->() RETURN count(r)")
    print(f"MANAGED_BY: {managed_count}, STARTS_ON: {starts_count}, ENDS_ON: {ends_count}")


# (vocab node label, relationship type, entity label, entity's natural key column,
#  source CSV, raw string column to canonicalize)
VOCAB_SPECS = [
    ("StoreType", "HAS_TYPE", "Store", "store_sk", "dim_stores.csv", "store_type"),
    ("CustomerSegment", "IN_SEGMENT", "Customer", "customer_sk", "dim_customers.csv", "customer_segment"),
    ("ProductCategory", "IN_CATEGORY", "Product", "product_sk", "dim_products.csv", "category"),
    ("SalespersonRole", "HAS_ROLE", "Salesperson", "salesperson_sk", "dim_salespersons.csv", "salesperson_role"),
]


def load_reference_vocabulary(driver):
    """Phase 3: canonical reference-vocabulary nodes for the four
    controlled-vocabulary columns. Canonicalizing via .strip() here (not in
    Cypher) fixes the 'Supermarkets ' trailing-space data-quality issue at
    the data layer — every future Cypher query against :StoreType.name gets
    the clean value for free, no TRIM() required. Purely additive: the raw
    string property stays on the entity node untouched."""
    for vocab_label, rel_type, entity_label, entity_key, filename, column in VOCAB_SPECS:
        pairs = [
            {"entity_sk": int(r[entity_key]), "name": r[column].strip()}
            for r in read_csv(filename)
        ]
        query = f"""
        UNWIND $rows AS row
        MERGE (v:{vocab_label} {{name: row.name}})
        WITH v, row
        MATCH (e:{entity_label} {{{entity_key}: row.entity_sk}})
        MERGE (e)-[:{rel_type}]->(v)
        """
        for batch in batches(pairs):
            driver.execute_query(query, rows=batch)
        vocab_count = scalar(driver, f"MATCH (v:{vocab_label}) RETURN count(v)")
        rel_count = scalar(driver, f"MATCH (:{entity_label})-[r:{rel_type}]->(:{vocab_label}) RETURN count(r)")
        print(f"{vocab_label}: {vocab_count} nodes, {rel_count} {rel_type} relationships")


def load_derived_properties(driver):
    """Phase 3: materialized derived properties, computed once at load time
    from data already in the graph (no CSV re-read needed).

    - Sale.within_campaign_window: BUSINESS_RULES.md's least obvious rule
      (campaign attribution is FK-only, NOT date-bounded) made directly
      queryable instead of requiring the agent to reconstruct the
      date-range join+comparison itself.
    - Customer/Salesperson.display_name: structurally disambiguates
      identity (see the "Michael Davis" non-unique-name trap in
      CLAUDE.md/BUSINESS_RULES.md) so even a naive RETURN of just the
      display name can't silently merge different people.
    """
    # CALL {...} IN TRANSACTIONS requires an implicit (auto-commit) transaction,
    # which driver.execute_query doesn't provide (it wraps queries in an explicit
    # transaction function) — use session.run() directly instead.
    with driver.session() as session:
        session.run(
            """
            MATCH (s:Sale)-[:PART_OF_CAMPAIGN]->(c:Campaign)-[:STARTS_ON]->(d1:Date)
            MATCH (c)-[:ENDS_ON]->(d2:Date)
            CALL (s, d1, d2) {
                SET s.within_campaign_window = (date(s.sales_date) >= d1.full_date AND date(s.sales_date) <= d2.full_date)
            } IN TRANSACTIONS OF 10000 ROWS
            """
        ).consume()
    total = scalar(driver, "MATCH (s:Sale) WHERE s.within_campaign_window IS NOT NULL RETURN count(s)")
    in_window = scalar(driver, "MATCH (s:Sale {within_campaign_window: true}) RETURN count(s)")
    print(f"Sale.within_campaign_window: set on {total} sales, {in_window} within window ({in_window / total:.1%})")

    driver.execute_query(
        "MATCH (c:Customer) SET c.display_name = c.first_name + ' ' + c.last_name + ' (' + c.customer_id + ')'"
    )
    driver.execute_query(
        "MATCH (sp:Salesperson) SET sp.display_name = sp.salesperson_name + ' (' + sp.salesperson_id + ')'"
    )
    print("Customer.display_name and Salesperson.display_name set.")


BUSINESS_RULES = [
    {
        "id": "campaign_attribution_not_date_bounded",
        "title": "Campaign attribution is FK-only, not date-bounded",
        "description": (
            "A sale is attributed to a campaign purely via the campaign_sk "
            "foreign key (the PART_OF_CAMPAIGN relationship) - not by whether "
            "the sale's date falls within that campaign's advertised "
            "start/end dates. Only ~39.5% of campaign-attributed sales "
            "actually occur within their campaign's date window (see the "
            "Sale.within_campaign_window property). Never filter or assume "
            "'sales during a campaign' means date-bounded unless the question "
            "explicitly asks about the campaign's advertised time period."
        ),
        "applies_to": ["Sale", "Campaign"],
    },
    {
        "id": "names_not_unique_disambiguate",
        "title": "Display names are not unique - always disambiguate individuals",
        "description": (
            "Multiple different customers or salespersons can share the exact "
            "same display name (e.g. 3 different salespersons are all named "
            "'Michael Davis', each a distinct person with a different "
            "salesperson_sk). Always identify or group individuals by "
            "surrogate key or the display_name property (which embeds the "
            "natural id), never by the raw name property alone - grouping by "
            "name alone silently merges different people's data together."
        ),
        "applies_to": ["Customer", "Salesperson"],
    },
    {
        "id": "no_organic_sales",
        "title": "Every sale has a campaign - there is no non-campaign sale",
        "description": (
            "All 1,000,000 sales in this dataset are attributed to some "
            "marketing campaign; there is no 'organic, no-campaign' sale "
            "subset. A question asking how many sales have no linked "
            "campaign is answerable immediately as zero, with no need to "
            "scan the Sale nodes."
        ),
        "applies_to": ["Sale", "Campaign"],
    },
    {
        "id": "budget_vs_revenue_not_comparable",
        "title": "Campaign budget and attributed revenue are not on the same scale",
        "description": (
            "Campaign.campaign_budget (a planning figure, ~$1M typical) and a "
            "campaign's attributed revenue (summed actual Sale.total_amount, "
            "~$55M typical) are different units of measure - a budget is not "
            "a spend figure directly comparable to revenue. Do not compute or "
            "imply a 'return on budget'/utilization ratio unless the user "
            "explicitly asks for that specific analysis."
        ),
        "applies_to": ["Campaign"],
    },
    {
        "id": "single_year_dataset",
        "title": "Dataset covers exactly one calendar year (2024)",
        "description": (
            "All Sale and Date data covers a single calendar year, 2024 "
            "(366 days, a leap year). Year-scoped questions never need an "
            "explicit year filter beyond what's already implied, and no "
            "cross-year comparison is possible with this data."
        ),
        "applies_to": ["Sale", "Date"],
    },
]


def load_business_rules(driver):
    """Phase 3: queryable knowledge-graph rule layer. These are rules that
    don't fully reduce to structure even after the reference-vocabulary and
    derived-property enrichments above - interpretive guidance the agent
    should consult. Kept as graph data (not hardcoded only in
    GRAPH_SCHEMA_DESCRIPTION) so new rules can be added later by inserting
    nodes, without redeploying agent code."""
    driver.execute_query(
        "UNWIND $rows AS row MERGE (r:BusinessRule {id: row.id}) SET r += row",
        rows=BUSINESS_RULES,
    )
    count = scalar(driver, "MATCH (r:BusinessRule) RETURN count(r)")
    print(f"BusinessRule: {count} nodes")


def main():
    driver = get_driver()
    try:
        apply_schema(driver)
        load_dimension_nodes(driver)
        load_sales(driver)
        load_dim_relationships(driver)
        load_reference_vocabulary(driver)
        load_derived_properties(driver)
        load_business_rules(driver)
    finally:
        driver.close()
    print("Done.")


if __name__ == "__main__":
    main()
