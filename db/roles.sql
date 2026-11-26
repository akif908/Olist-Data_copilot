-- ============================================================================
-- Data Analyst Copilot — Phase 5 dedicated read-only role (security layer L3).
--
-- This is the INDEPENDENT security boundary: even if the LLM and the SQLGlot
-- validator (L2) are bypassed, this role can only SELECT the seven verified
-- tables, cannot write/DDL, cannot create temp tables, and runs under strict
-- timeouts. Reference template; scripts/setup_readonly_user.py substitutes the
-- password from PG_RO_PASSWORD (never hard-coded) and is idempotent.
--
-- The placeholder :ro_password is replaced at run time with a safely-quoted
-- literal. The placeholder :dbname is the target database (default olist_copilot).
-- ============================================================================

-- 1. Dedicated login role with NO dangerous attributes.
--    Idempotent: created if absent, otherwise altered to the same safe state.
CREATE ROLE copilot_ro LOGIN PASSWORD :'ro_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- 2. Allow connecting; remove TEMPORARY from PUBLIC so no non-superuser role
--    (including copilot_ro) can create temporary tables. This is the only
--    database-wide privilege change; verified safe (sole login role is the
--    postgres superuser, which bypasses ACLs; no temp-table dependency exists).
GRANT CONNECT ON DATABASE :dbname TO copilot_ro;
REVOKE TEMPORARY ON DATABASE :dbname FROM PUBLIC;

-- 3. Read access to exactly the seven verified Olist tables (no more).
GRANT USAGE ON SCHEMA public TO copilot_ro;
GRANT SELECT ON
    customers, orders, order_items, order_payments,
    order_reviews, products, product_category_translation
TO copilot_ro;

-- 4. Role-level defense-in-depth defaults, enforced by the database on every
--    session regardless of what the application sends.
ALTER ROLE copilot_ro SET default_transaction_read_only = on;
ALTER ROLE copilot_ro SET statement_timeout = '10s';
ALTER ROLE copilot_ro SET idle_in_transaction_session_timeout = '30s';
ALTER ROLE copilot_ro SET lock_timeout = '5s';

-- Note: table ACLs are owner-only by default and there are no default-privilege
-- rows, so future tables are NOT automatically readable by copilot_ro. New
-- tables require an explicit GRANT SELECT (keeps the allowlist authoritative).