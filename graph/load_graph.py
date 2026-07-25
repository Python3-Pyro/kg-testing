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


def main():
    driver = get_driver()
    try:
        apply_schema(driver)
        load_dimension_nodes(driver)
        load_sales(driver)
        load_dim_relationships(driver)
    finally:
        driver.close()
    print("Done.")


if __name__ == "__main__":
    main()
