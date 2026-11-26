# PostgreSQL privilege audit (read-only)

Generated: 2026-11-25T13:27:28.857593+00:00

All queries are catalog SELECTs run in a read-only transaction; nothing was modified. This captures the isolation state before creating copilot_ro.

## Current connection identity

| current_user | session_user | is_superuser |
|---|---|---|
| postgres | postgres | True |

## Roles and role-level attributes

| rolname | rolsuper | rolcreaterole | rolcreatedb | rolreplication | rolbypassrls | rolcanlogin |
|---|---|---|---|---|---|---|
| pg_checkpoint | False | False | False | False | False | False |
| pg_create_subscription | False | False | False | False | False | False |
| pg_database_owner | False | False | False | False | False | False |
| pg_execute_server_program | False | False | False | False | False | False |
| pg_maintain | False | False | False | False | False | False |
| pg_monitor | False | False | False | False | False | False |
| pg_read_all_data | False | False | False | False | False | False |
| pg_read_all_settings | False | False | False | False | False | False |
| pg_read_all_stats | False | False | False | False | False | False |
| pg_read_server_files | False | False | False | False | False | False |
| pg_signal_autovacuum_worker | False | False | False | False | False | False |
| pg_signal_backend | False | False | False | False | False | False |
| pg_stat_scan_tables | False | False | False | False | False | False |
| pg_use_reserved_connections | False | False | False | False | False | False |
| pg_write_all_data | False | False | False | False | False | False |
| pg_write_server_files | False | False | False | False | False | False |
| postgres | True | True | True | True | True | True |

## Role memberships (inheritance paths)

| member | granted_role | admin_option |
|---|---|---|
| pg_monitor | pg_read_all_settings | False |
| pg_monitor | pg_read_all_stats | False |
| pg_monitor | pg_stat_scan_tables | False |

## Database ownership and ACL (CONNECT / TEMPORARY)

| datname | owner | datacl |
|---|---|---|
| olist_copilot | postgres |  |

## Schema ownership and ACL (public)

| nspname | owner | nspacl |
|---|---|---|
| public | pg_database_owner | ['pg_database_owner=UC/pg_database_owner', '=U/pg_database_owner'] |

## Table ownership (public)

| tablename | tableowner |
|---|---|
| customers | postgres |
| order_items | postgres |
| order_payments | postgres |
| order_reviews | postgres |
| orders | postgres |
| product_category_translation | postgres |
| products | postgres |

## Table ACLs (public) — look for PUBLIC ('=') grants

| relname | relacl |
|---|---|
| customers |  |
| order_items |  |
| order_payments |  |
| order_reviews |  |
| orders |  |
| product_category_translation |  |
| products |  |

## Default privileges (future objects)

_(no rows)_

## Existing copilot_ro role (if any)

_(no rows)_
