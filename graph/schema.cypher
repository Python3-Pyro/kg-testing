CREATE CONSTRAINT customer_sk_unique IF NOT EXISTS FOR (c:Customer) REQUIRE c.customer_sk IS UNIQUE;
CREATE CONSTRAINT product_sk_unique IF NOT EXISTS FOR (p:Product) REQUIRE p.product_sk IS UNIQUE;
CREATE CONSTRAINT store_sk_unique IF NOT EXISTS FOR (s:Store) REQUIRE s.store_sk IS UNIQUE;
CREATE CONSTRAINT salesperson_sk_unique IF NOT EXISTS FOR (sp:Salesperson) REQUIRE sp.salesperson_sk IS UNIQUE;
CREATE CONSTRAINT campaign_sk_unique IF NOT EXISTS FOR (c:Campaign) REQUIRE c.campaign_sk IS UNIQUE;
CREATE CONSTRAINT date_sk_unique IF NOT EXISTS FOR (d:Date) REQUIRE d.date_sk IS UNIQUE;
CREATE CONSTRAINT sale_id_unique IF NOT EXISTS FOR (s:Sale) REQUIRE s.sales_id IS UNIQUE;
