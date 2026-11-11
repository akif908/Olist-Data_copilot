# Business Glossary — Olist Copilot

Currency: BRL. All definitions verified against the live olist_copilot database. Monetary metrics are GROSS: the dataset has no refund, return, or cost fields, so net revenue and profit cannot be computed reliably. Every metric states its filters and caveats explicitly.

Source of truth: `knowledge_base/business_glossary/glossary.json`.
Rendered by `python -m scripts.build_metadata`.

## Order count (`order_count`)

Number of orders placed, counting every row in orders regardless of status.

```sql
SELECT count(*) AS order_count FROM orders
```

- **Unit:** orders
- **Tables:** orders
- **Filters:** none — all 8 statuses included
- **Caveats:**
  - Includes 625 canceled and 609 unavailable orders; an order row is not a completed sale.
  - For completed sales use the delivered_orders metric instead.

## Customer count (`customer_count`)

Number of distinct real customers, computed as COUNT(DISTINCT customer_unique_id) on customers.

```sql
SELECT count(DISTINCT customer_unique_id) AS customer_count FROM customers
```

- **Unit:** customers
- **Tables:** customers
- **Filters:** none
- **Caveats:**
  - Never count customer_id: it identifies order-side records (99,441), not people (96,096).
  - A person who placed several orders is counted once.

## Average order value (AOV) (`average_order_value`)

Average total paid per order: SUM(payment_value) divided by COUNT(DISTINCT order_id) over order_payments. Includes freight.

```sql
SELECT round(sum(payment_value) / count(DISTINCT order_id), 2) AS average_order_value FROM order_payments
```

- **Unit:** BRL per order
- **Tables:** order_payments
- **Filters:** orders with at least one payment row
- **Caveats:**
  - Exactly one order has no payment row and is excluded from this metric.
  - All 625 canceled and 609 unavailable orders DO have payment rows, so their payment values are included. For an AOV of completed orders only, join orders and filter order_status = 'delivered'.
  - Includes freight; it is not a merchandise-only average.
  - Gross of refunds: refunds are not observable in this dataset.
  - Merchandise-only variant: SUM(price) / COUNT(DISTINCT order_id) over order_items (excludes freight).

## Gross item value (`gross_item_value`)

Total merchandise value of all order items: SUM(price) over order_items. Excludes freight.

```sql
SELECT sum(price) AS gross_item_value FROM order_items
```

- **Unit:** BRL
- **Tables:** order_items
- **Filters:** none — items of all orders included
- **Caveats:**
  - This is NOT net revenue: the dataset has no refund or return fields.
  - Includes items belonging to canceled and unavailable orders. For a delivered-only variant, join orders and filter order_status = 'delivered'.
  - Each row is one unit; SUM(price) already accounts for quantity.

## Freight value (`freight_value`)

Total shipping value charged to customers: SUM(freight_value) over order_items.

```sql
SELECT sum(freight_value) AS total_freight_value FROM order_items
```

- **Unit:** BRL
- **Tables:** order_items
- **Filters:** none
- **Caveats:**
  - Freight is allocated per item; summing an order's items gives the order's total shipping charge.
  - This is the price charged to the customer, not the carrier's actual cost.

## Delivered orders (`delivered_orders`)

Number of orders whose final status is 'delivered'.

```sql
SELECT count(*) AS delivered_orders FROM orders WHERE order_status = 'delivered'
```

- **Unit:** orders
- **Tables:** orders
- **Filters:** order_status = 'delivered'
- **Caveats:**
  - Status is a final snapshot; the dataset has no status history.
  - 8 delivered orders have a NULL order_delivered_customer_date; status-based counting still includes them.

## Cancellation rate (`cancellation_rate`)

Share of all orders whose final status is 'canceled', as a percentage: 100 * canceled orders / all orders.

```sql
SELECT round(100.0 * count(*) FILTER (WHERE order_status = 'canceled') / count(*), 2) AS cancellation_rate_pct FROM orders
```

- **Unit:** percent
- **Tables:** orders
- **Filters:** numerator: order_status = 'canceled'; denominator: all orders
- **Caveats:**
  - 'unavailable' (609 orders) is a distinct outcome and is NOT counted as canceled. A broader 'not fulfilled' rate would need its own definition.
  - The rate is over all orders in the dataset, not per time period; add a purchase-timestamp filter for period-specific rates.

## Average delivery time (`average_delivery_time`)

Average number of days from purchase to customer receipt for delivered orders: AVG(order_delivered_customer_date - order_purchase_timestamp), in days.

```sql
SELECT round(avg(EXTRACT(EPOCH FROM (order_delivered_customer_date - order_purchase_timestamp)) / 86400.0), 2) AS avg_delivery_days FROM orders WHERE order_status = 'delivered' AND order_delivered_customer_date IS NOT NULL
```

- **Unit:** days
- **Tables:** orders
- **Filters:** order_status = 'delivered' AND order_delivered_customer_date IS NOT NULL
- **Caveats:**
  - Includes seller processing time plus carrier transit; it is purchase-to-door, not transit-only.
  - Excludes orders without a customer-delivery timestamp, including 8 orders marked delivered.
  - Transit-only variant: order_delivered_customer_date - order_delivered_carrier_date.

## Review score (`review_score`)

Mean review score over all review rows: AVG(review_score) on order_reviews, on a 1-5 scale.

```sql
SELECT round(avg(review_score), 2) AS avg_review_score FROM order_reviews
```

- **Unit:** stars (1-5)
- **Tables:** order_reviews
- **Filters:** none
- **Caveats:**
  - Averaged per review, not per order: 547 orders have more than one review.
  - Selection bias: only customers who chose to review are represented.
  - review_id repeats across orders (1,603 rows); count review rows, not DISTINCT review_id.

## Metrics the dataset cannot support

- **net_revenue**: The dataset has no refund, return, or chargeback fields. SUM(price) or SUM(payment_value) are gross values only. Any 'net revenue' answer must state this limitation instead of guessing.
- **profit**: No product cost, carrier cost, or fee fields exist. Profit and margin cannot be computed from this dataset.
