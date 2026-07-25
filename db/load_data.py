"""Creates the star-schema tables and bulk-loads the CSVs in data/ into Postgres.

Usage: python db/load_data.py
Requires the Postgres container to be up (docker compose up -d) and .env configured.
"""
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
SCHEMA_FILE = ROOT / "db" / "schema.sql"

# (table, csv filename, columns in load order)
TABLES = [
    ("dim_dates", "dim_dates.csv",
     ["full_date", "date_sk", "year", "month", "day", "weekday", "quarter"]),
    ("dim_salespersons", "dim_salespersons.csv",
     ["salesperson_sk", "salesperson_id", "salesperson_name", "salesperson_role"]),
    ("dim_customers", "dim_customers.csv",
     ["customer_sk", "customer_id", "first_name", "last_name", "email",
      "residential_location", "customer_segment"]),
    ("dim_products", "dim_products.csv",
     ["product_sk", "product_id", "product_name", "category", "brand", "origin_location"]),
    ("dim_stores", "dim_stores.csv",
     ["store_sk", "store_id", "store_name", "store_type", "store_location", "store_manager_sk"]),
    ("dim_campaigns", "dim_campaigns.csv",
     ["campaign_sk", "campaign_id", "campaign_name", "start_date_sk", "end_date_sk", "campaign_budget"]),
    ("fact_sales", "fact_sales_normalized.csv",
     ["sales_sk", "sales_id", "customer_sk", "product_sk", "store_sk",
      "salesperson_sk", "campaign_sk", "sales_date", "total_amount"]),
]


def get_connection():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def main():
    conn = get_connection()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            print("Applying schema...")
            cur.execute(SCHEMA_FILE.read_text())

            for table, csv_name, columns in TABLES:
                csv_path = DATA_DIR / csv_name
                col_list = ", ".join(columns)
                copy_sql = f"COPY {table} ({col_list}) FROM STDIN WITH (FORMAT csv, HEADER true)"
                print(f"Loading {table} from {csv_name}...")
                with open(csv_path, "r", encoding="utf-8") as f:
                    cur.copy_expert(copy_sql, f)
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                print(f"  -> {count} rows in {table}")

        conn.commit()
        print("Done.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
