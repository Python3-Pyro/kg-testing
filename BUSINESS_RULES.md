# Business Rules — Retail Star Schema

Derived from the 20-question ground-truth eval set (`eval/ground_truth.json`) and empirical review of the loaded data (Postgres `fact_sales` + dims, 1,000,000 sales / 100,000 customers / 500 stores / 2,000 salespersons / 50 campaigns / 2024 calendar year). These are the definitions and gotchas both the SQL and Graph agents need to get right to answer questions correctly — several were found by directly querying the data, not assumed.

## Core metrics

- **Total revenue** = `SUM(total_amount)` over `fact_sales`, optionally filtered/grouped. There is no separate "revenue" column or table — every revenue question reduces to summing `total_amount`.
- **Transaction count** = `COUNT(*)` over `fact_sales`. Every row is exactly one sale; there is no cancellation/return/void state to exclude.
- **Average sale amount** = `AVG(total_amount)`, overall or filtered by any dimension (segment, category, store type, ...).
- **"Top N" of an entity** (salesperson, customer, store, product, category, campaign) = rank by `SUM(total_amount)` attributed to that entity via its foreign key on `fact_sales`, descending.
- `total_amount` ranges from **$500.00 to $4,999.98** — no zero, negative, or unbounded values. A result outside this band on a per-sale question signals a query bug, not real data.

## Attribution rules (how a sale "belongs" to a dimension)

- A sale is attributed to a customer, product, store, salesperson, or campaign **purely by the foreign key on `fact_sales`** (`customer_sk`, `product_sk`, `store_sk`, `salesperson_sk`, `campaign_sk`). No other condition applies.
- **Campaign attribution is not date-bounded** — this is the least obvious rule in the whole schema, confirmed empirically: only **394,920 of 1,000,000 sales (39.5%)** fall within their linked campaign's `start_date`–`end_date` window. A sale credited to a campaign frequently happened well outside that campaign's advertised run. Do **not** filter "sales during campaign X" by date range — attribution is the FK alone, full stop.
- Every sale has a complete set of dimension FKs — zero NULLs found across `customer_sk/product_sk/store_sk/salesperson_sk/campaign_sk/total_amount` in all 1,000,000 rows. In particular, **100% of sales are attributed to some campaign**; there's no "organic, non-campaign" sale in this dataset (unrealistic for real retail, but true here — don't assume a subset of sales needs a "no campaign" filter).
- `campaign_budget` (~$1M per campaign) and campaign-attributed revenue (~$55M per campaign) are **different units of measure, not comparable as spend-vs-return** — budget is a planning figure on `dim_campaigns`, revenue is summed actuals from `fact_sales`. Don't compute a "ROI" or "budget utilization %" implying they're on the same scale unless the user explicitly wants that (synthetic, ~55x) ratio.
- Every store has exactly one manager (`store_manager_sk`, `NOT NULL`), and empirically **every store manager holds `salesperson_role = 'Manager'`** (500/500) — consistent in the data, though not DB-enforced. Don't assume this generalizes if the dataset is ever regenerated/extended.

## Identity rules

- **Display names are not unique — never group or rank by name alone.** Confirmed duplicates: e.g. 3 different salespersons named "Michael Davis," each with a different `salesperson_sk`. Grouping by `salesperson_name` (SQL) or returning just a name (Cypher, where non-aggregated `RETURN` fields become the implicit group-by) silently merges distinct people's sales into one inflated, misattributed total. Always group/return the surrogate key (`salesperson_sk`, `customer_sk`, ...) alongside the display name for any "top N individual" question. (This bug was caught by the eval on `gt_05` and fixed in both agents' system prompts — see `CLAUDE.md` Conventions.)
- Surrogate keys (`*_sk`) are the real identity for joins and grouping; natural/business ids (`customer_id`, `product_id`, ...) are display-only labels, unique but not used structurally by either agent.

## Time rules

- The dataset covers a single calendar year, **2024** (366 days — a leap year), so year-scoped questions never need a year filter beyond what's already implied.
- `fact_sales.sales_date` is a full timestamp **not foreign-keyed to `dim_dates`** — there is no `date_sk` column on the fact table (SQL) and no `Sale`→`Date` relationship (graph). Calendar attributes (quarter, weekday, month) must be derived directly from `sales_date` (SQL: `EXTRACT`/cast + join `dim_dates` on `sales_date::date = full_date` if named weekday/quarter labels are needed from the dimension; Cypher: native accessors like `date(s.sales_date).quarter` work directly, no join needed at all — simpler than the SQL path for this specific case).
- `dim_dates` itself is only really needed for `dim_campaigns.start_date_sk`/`end_date_sk` lookups (campaign date range) — not for classifying individual sales, per the attribution rule above.

## Reference values (exact, case- and space-sensitive)

- **`customer_segment`** (10): First-time Buyer, Churn Risk, In-Store Regular, Occasional Shopper, Premium Shopper, Budget Shopper, Deal Seeker, Loyal Customer, Online Shopper, High Value
- **`category`** (6): Clothing, Groceries, Sports & Outdoors, Furniture, Home Appliances, Electronics
- **`store_type`** (3): `Small Stores / Shops`, `Large Malls / Complexes`, and **`Supermarkets ` — has a trailing space** in the source data. An exact-match filter on `store_type = 'Supermarkets'` (no trailing space) will silently return zero rows; use `TRIM(store_type) = 'Supermarkets'` or match the literal trailing-space value.
- **`salesperson_role`** (4): Salesperson, Senior Salesperson, Sales Associate, Manager

## Table/data source rules

- `fact_sales_denormalized.csv` is **not loaded** anywhere (not in Postgres, not in the graph) — it inlines dimension columns onto the fact table, which would let an agent skip real joins/traversals and never exercise (or validate) the actual star-schema relationships. `fact_sales_normalized.csv` (→ `fact_sales` table / `:Sale` nodes) is the only source of truth for sales.
- Postgres (`fact_sales` + dims) is the ground-truth oracle for eval scoring (`eval/run_eval.py`'s `oracle`) regardless of which backend (SQL or Graph) is being tested — the graph is a second representation of the same facts, not an independent source.
