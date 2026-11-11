-- ============================================================================
-- Data Analyst Copilot — Phase 1 schema (PostgreSQL 18, database olist_copilot)
-- Source: Olist Brazilian E-Commerce Public Dataset (Kaggle).
-- Column names match the source CSVs exactly (including the source's
-- "lenght" misspelling) so COPY loading is lossless and traceable.
-- Design decisions: docs/schema.md. Profiling facts: docs/data_inspection.md.
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS).
-- ============================================================================

-- One row per order-side customer record. customer_id is the PK used by
-- orders; customer_unique_id identifies the actual person (not unique here).
CREATE TABLE IF NOT EXISTS customers (
    customer_id              varchar(32)  PRIMARY KEY,
    customer_unique_id       varchar(32)  NOT NULL,
    customer_zip_code_prefix integer      NOT NULL,
    customer_city            varchar(100) NOT NULL,
    customer_state           varchar(2)   NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id                      varchar(32) PRIMARY KEY,
    customer_id                   varchar(32) NOT NULL REFERENCES customers (customer_id),
    order_status                  varchar(20) NOT NULL,
    order_purchase_timestamp      timestamp   NOT NULL,
    order_approved_at             timestamp,
    order_delivered_carrier_date  timestamp,
    order_delivered_customer_date timestamp,
    order_estimated_delivery_date timestamp   NOT NULL
);

-- Lookup table: Portuguese category name -> English name. Not a strict
-- dimension: 13 products use 2 categories absent from this file, so
-- products.product_category_name has NO enforced FK (use LEFT JOIN).
CREATE TABLE IF NOT EXISTS product_category_translation (
    product_category_name         varchar(100) PRIMARY KEY,
    product_category_name_english varchar(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id                 varchar(32)  PRIMARY KEY,
    product_category_name      varchar(100),
    product_name_lenght        integer,
    product_description_lenght integer,
    product_photos_qty         integer,
    product_weight_g           integer,
    product_length_cm          integer,
    product_height_cm          integer,
    product_width_cm           integer
);

-- One row per item line of an order. seller_id is loaded without an FK
-- until the sellers table is imported (deferred beyond Phase 1).
CREATE TABLE IF NOT EXISTS order_items (
    order_id            varchar(32)   NOT NULL REFERENCES orders (order_id),
    order_item_id       integer       NOT NULL,
    product_id          varchar(32)   NOT NULL REFERENCES products (product_id),
    seller_id           varchar(32)   NOT NULL,
    shipping_limit_date timestamp     NOT NULL,
    price               numeric(10,2) NOT NULL,
    freight_value       numeric(10,2) NOT NULL,
    PRIMARY KEY (order_id, order_item_id)
);

-- One row per payment transaction; an order may have multiple payments.
CREATE TABLE IF NOT EXISTS order_payments (
    order_id             varchar(32)   NOT NULL REFERENCES orders (order_id),
    payment_sequential   integer       NOT NULL,
    payment_type         varchar(20)   NOT NULL,
    payment_installments integer       NOT NULL,
    payment_value        numeric(10,2) NOT NULL,
    PRIMARY KEY (order_id, payment_sequential)
);

-- One row per review. review_id alone is NOT unique in the source (1,603
-- rows share a review_id across different orders); (review_id, order_id)
-- was verified unique and is the primary key. Added in Phase 2 for the
-- review-score metric.
CREATE TABLE IF NOT EXISTS order_reviews (
    review_id               varchar(32) NOT NULL,
    order_id                varchar(32) NOT NULL REFERENCES orders (order_id),
    review_score            integer     NOT NULL CHECK (review_score BETWEEN 1 AND 5),
    review_comment_title    text,
    review_comment_message  text,
    review_creation_date    timestamp   NOT NULL,
    review_answer_timestamp timestamp   NOT NULL,
    PRIMARY KEY (review_id, order_id)
);

-- Indexes for the analytical access patterns of the first questions.
CREATE INDEX IF NOT EXISTS idx_orders_customer_id    ON orders (customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status         ON orders (order_status);
CREATE INDEX IF NOT EXISTS idx_orders_purchase_ts    ON orders (order_purchase_timestamp);
CREATE INDEX IF NOT EXISTS idx_order_items_product   ON order_items (product_id);
CREATE INDEX IF NOT EXISTS idx_order_items_seller    ON order_items (seller_id);
CREATE INDEX IF NOT EXISTS idx_products_category     ON products (product_category_name);
CREATE INDEX IF NOT EXISTS idx_customers_unique_id   ON customers (customer_unique_id);
CREATE INDEX IF NOT EXISTS idx_order_reviews_order   ON order_reviews (order_id);

-- Table comments feed the Phase 2 schema inspector (no fabricated metadata).
COMMENT ON TABLE customers IS 'Order-side customer records; one row per order. customer_unique_id identifies the actual customer (96,096 distinct across 99,441 rows).';
COMMENT ON TABLE orders IS 'One row per order with lifecycle timestamps. order_status: approved, canceled, created, delivered, invoiced, processing, shipped, unavailable.';
COMMENT ON TABLE product_category_translation IS 'Portuguese-to-English category lookup (71 rows). Incomplete: pc_gamer and portateis_cozinha_e_preparadores_de_alimentos are missing; join with LEFT JOIN.';
COMMENT ON TABLE products IS 'One row per product. Category and attribute columns are nullable; column names keep the source spelling (e.g. product_name_lenght).';
COMMENT ON TABLE order_items IS 'Order line items: quantity is represented by one row per unit (order_item_id). price is item value; freight_value is the shipping cost allocated to the item.';
COMMENT ON TABLE order_payments IS 'Payment transactions per order (order_id + payment_sequential). payment_value includes freight; one order in the dataset has no payment row.';
COMMENT ON TABLE order_reviews IS 'One row per review; PK (review_id, order_id) because review_id alone repeats across orders. review_score is 1-5. Comment title/message are mostly NULL.';

COMMENT ON COLUMN customers.customer_zip_code_prefix IS 'First digits of the CEP postal code. Source stores it as an integer, so leading zeros are lost (e.g. 1037 means 01037).';
COMMENT ON COLUMN products.product_category_name IS 'No enforced FK: 13 products reference categories missing from product_category_translation. 610 products have NULL category.';
COMMENT ON COLUMN order_items.seller_id IS 'FK to sellers deferred until the sellers table is imported; all values verified against the source sellers CSV.';