# Verified SQL Example Library

Every example below was executed against `olist_copilot` in a read-only
transaction by `python -m scripts.verify_sql_examples`. Results are the
actual database output, not estimates. Re-run the script to refresh.

## ex_order_count — How many orders are in the database?

```sql
SELECT count(*) AS order_count FROM orders
```

*Counts all statuses, including canceled and unavailable.*

**Result:** `99441`

*1 row(s) returned in 13.6 ms.*

## ex_customer_count — How many unique customers exist?

```sql
SELECT count(DISTINCT customer_unique_id) AS customer_count FROM customers
```

*Uses customer_unique_id (people), not customer_id (order-side records).*

**Result:** `96096`

*1 row(s) returned in 31.8 ms.*

## ex_top_categories_by_items — What are the top five product categories by order-item count?

```sql
SELECT COALESCE(t.product_category_name_english, p.product_category_name, '(unknown)') AS category, count(*) AS item_count FROM order_items i JOIN products p ON p.product_id = i.product_id LEFT JOIN product_category_translation t ON t.product_category_name = p.product_category_name GROUP BY 1 ORDER BY item_count DESC LIMIT 5
```

*LEFT JOIN because 2 categories lack translations; COALESCE falls back to the Portuguese name, then '(unknown)' for NULL categories.*

| category | item_count |
|---|---|
| bed_bath_table | 11115 |
| health_beauty | 9670 |
| sports_leisure | 8641 |
| furniture_decor | 8334 |
| computers_accessories | 7827 |

*5 row(s) returned in 147.0 ms.*

## ex_average_order_value — What is the average order value under the documented metric definition?

```sql
SELECT round(sum(payment_value) / count(DISTINCT order_id), 2) AS average_order_value FROM order_payments
```

*Payment-based AOV including freight; excludes the one order without payments; includes payments of canceled orders (see glossary caveats).*

**Result:** `160.99`

*1 row(s) returned in 93.8 ms.*

## ex_canceled_orders — How many orders were cancelled?

```sql
SELECT count(*) AS canceled_orders FROM orders WHERE order_status = 'canceled'
```

*'unavailable' orders are a separate outcome and are not counted here.*

**Result:** `625`

*1 row(s) returned in 1.5 ms.*

## ex_gross_item_value — What is the total gross item value?

```sql
SELECT sum(price) AS gross_item_value FROM order_items
```

*Merchandise value only, excludes freight. Not net revenue.*

**Result:** `13591643.70`

*1 row(s) returned in 23.4 ms.*

## ex_freight_value — What is the total freight value charged?

```sql
SELECT sum(freight_value) AS total_freight_value FROM order_items
```

*Sum of per-item shipping allocations.*

**Result:** `2251909.54`

*1 row(s) returned in 21.7 ms.*

## ex_delivered_orders — How many orders were delivered?

```sql
SELECT count(*) AS delivered_orders FROM orders WHERE order_status = 'delivered'
```

*Status-based count; includes the 8 delivered orders with NULL delivery date.*

**Result:** `96478`

*1 row(s) returned in 8.3 ms.*

## ex_cancellation_rate — What is the cancellation rate?

```sql
SELECT round(100.0 * count(*) FILTER (WHERE order_status = 'canceled') / count(*), 2) AS cancellation_rate_pct FROM orders
```

*Canceled orders as a percentage of all orders.*

**Result:** `0.63`

*1 row(s) returned in 10.3 ms.*

## ex_average_delivery_time — What is the average delivery time in days?

```sql
SELECT round(avg(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_purchase_timestamp)) / 86400.0), 2) AS avg_delivery_days FROM orders WHERE order_status = 'delivered' AND order_delivered_customer_date IS NOT NULL
```

*Purchase-to-door days for delivered orders with a delivery timestamp.*

**Result:** `12.56`

*1 row(s) returned in 54.1 ms.*

## ex_review_score — What is the average review score?

```sql
SELECT round(avg(review_score), 2) AS avg_review_score FROM order_reviews
```

*Mean over review rows on a 1-5 scale; per-review, not per-order.*

**Result:** `4.09`

*1 row(s) returned in 18.6 ms.*

## ex_revenue_by_category — Which five product categories generated the highest revenue?

```sql
SELECT COALESCE(t.product_category_name_english, p.product_category_name, '(unknown)') AS category, sum(i.price) AS gross_item_value FROM order_items i JOIN products p ON p.product_id = i.product_id LEFT JOIN product_category_translation t ON t.product_category_name = p.product_category_name GROUP BY 1 ORDER BY gross_item_value DESC LIMIT 5
```

*'Revenue' is interpreted as gross item value per the glossary (merchandise, excludes freight, gross of refunds). The answer must state this interpretation.*

| category | gross_item_value |
|---|---|
| health_beauty | 1258681.34 |
| watches_gifts | 1205005.68 |
| bed_bath_table | 1036988.68 |
| sports_leisure | 988048.97 |
| computers_accessories | 911954.32 |

*5 row(s) returned in 103.3 ms.*

## ex_monthly_orders — How many orders were placed per month?

```sql
SELECT date_trunc('month', order_purchase_timestamp)::date AS month, count(*) AS order_count FROM orders GROUP BY 1 ORDER BY 1
```

*Time series suitable for a line chart (Phase 7).*

| month | order_count |
|---|---|
| 2016-09-01 | 4 |
| 2016-10-01 | 324 |
| 2016-12-01 | 1 |
| 2017-01-01 | 800 |
| 2017-02-01 | 1780 |
| 2017-03-01 | 2682 |
| 2017-04-01 | 2404 |
| 2017-05-01 | 3700 |
| 2017-06-01 | 3245 |
| 2017-07-01 | 4026 |
| 2017-08-01 | 4331 |
| 2017-09-01 | 4285 |
| 2017-10-01 | 4631 |
| 2017-11-01 | 7544 |
| 2017-12-01 | 5673 |
| 2018-01-01 | 7269 |
| 2018-02-01 | 6728 |
| 2018-03-01 | 7211 |
| 2018-04-01 | 6939 |
| 2018-05-01 | 6873 |
| 2018-06-01 | 6167 |
| 2018-07-01 | 6292 |
| 2018-08-01 | 6512 |
| 2018-09-01 | 16 |
| 2018-10-01 | 4 |

*25 row(s) returned in 36.9 ms.*
