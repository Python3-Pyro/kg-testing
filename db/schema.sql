DROP TABLE IF EXISTS fact_sales CASCADE;
DROP TABLE IF EXISTS dim_campaigns CASCADE;
DROP TABLE IF EXISTS dim_stores CASCADE;
DROP TABLE IF EXISTS dim_products CASCADE;
DROP TABLE IF EXISTS dim_customers CASCADE;
DROP TABLE IF EXISTS dim_salespersons CASCADE;
DROP TABLE IF EXISTS dim_dates CASCADE;

CREATE TABLE dim_dates (
    date_sk     INTEGER PRIMARY KEY,
    full_date   DATE NOT NULL,
    year        INTEGER NOT NULL,
    month       INTEGER NOT NULL,
    day         INTEGER NOT NULL,
    weekday     INTEGER NOT NULL,
    quarter     INTEGER NOT NULL
);

CREATE TABLE dim_salespersons (
    salesperson_sk      INTEGER PRIMARY KEY,
    salesperson_id       TEXT NOT NULL UNIQUE,
    salesperson_name     TEXT NOT NULL,
    salesperson_role     TEXT NOT NULL
);

CREATE TABLE dim_customers (
    customer_sk           INTEGER PRIMARY KEY,
    customer_id           TEXT NOT NULL UNIQUE,
    first_name            TEXT NOT NULL,
    last_name             TEXT NOT NULL,
    email                 TEXT NOT NULL,
    residential_location  TEXT NOT NULL,
    customer_segment      TEXT NOT NULL
);

CREATE TABLE dim_products (
    product_sk       INTEGER PRIMARY KEY,
    product_id       TEXT NOT NULL UNIQUE,
    product_name     TEXT NOT NULL,
    category         TEXT NOT NULL,
    brand            TEXT NOT NULL,
    origin_location  TEXT NOT NULL
);

CREATE TABLE dim_stores (
    store_sk          INTEGER PRIMARY KEY,
    store_id          TEXT NOT NULL UNIQUE,
    store_name        TEXT NOT NULL,
    store_type        TEXT NOT NULL,
    store_location    TEXT NOT NULL,
    store_manager_sk  INTEGER NOT NULL REFERENCES dim_salespersons(salesperson_sk)
);

CREATE TABLE dim_campaigns (
    campaign_sk      INTEGER PRIMARY KEY,
    campaign_id      TEXT NOT NULL UNIQUE,
    campaign_name    TEXT NOT NULL,
    start_date_sk    INTEGER NOT NULL REFERENCES dim_dates(date_sk),
    end_date_sk      INTEGER NOT NULL REFERENCES dim_dates(date_sk),
    campaign_budget  NUMERIC(12, 2) NOT NULL
);

CREATE TABLE fact_sales (
    sales_sk        INTEGER PRIMARY KEY,
    sales_id        TEXT NOT NULL UNIQUE,
    customer_sk     INTEGER NOT NULL REFERENCES dim_customers(customer_sk),
    product_sk      INTEGER NOT NULL REFERENCES dim_products(product_sk),
    store_sk        INTEGER NOT NULL REFERENCES dim_stores(store_sk),
    salesperson_sk  INTEGER NOT NULL REFERENCES dim_salespersons(salesperson_sk),
    campaign_sk     INTEGER NOT NULL REFERENCES dim_campaigns(campaign_sk),
    sales_date      TIMESTAMP NOT NULL,
    total_amount    NUMERIC(12, 2) NOT NULL
);

CREATE INDEX idx_fact_sales_customer ON fact_sales(customer_sk);
CREATE INDEX idx_fact_sales_product ON fact_sales(product_sk);
CREATE INDEX idx_fact_sales_store ON fact_sales(store_sk);
CREATE INDEX idx_fact_sales_salesperson ON fact_sales(salesperson_sk);
CREATE INDEX idx_fact_sales_campaign ON fact_sales(campaign_sk);
CREATE INDEX idx_fact_sales_date ON fact_sales(sales_date);
