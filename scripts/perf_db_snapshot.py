"""Phase-0 performance observability: a read-only Postgres health snapshot.

Queries only Postgres's built-in statistics views (pg_stat_activity,
pg_stat_database, pg_stat_user_tables) — none of this requires the
pg_stat_statements extension, so it needs NO config change and NO restart.
Safe to run against any environment, including production, since it only
reads system catalogs (no table data, no application rows).

    python -m scripts.perf_db_snapshot

See docs/PERFORMANCE_BASELINE.md for how this fits into the Phase-0 baseline,
and docs/PERFORMANCE_IMPLEMENTATION_LOG.md for why pg_stat_statements and
log_min_duration_statement are NOT enabled by this script (both would need a
config change + restart of the production Postgres container, which is
explicitly out of scope without separate, explicit approval).
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.core.config import get_settings

_ACTIVE_CONNECTIONS = """
SELECT state, count(*) AS count
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY state
ORDER BY count DESC
"""

_CACHE_HIT_RATIO = """
SELECT
    sum(blks_hit)  AS blocks_hit,
    sum(blks_read) AS blocks_read,
    round(
        100.0 * sum(blks_hit) / GREATEST(sum(blks_hit) + sum(blks_read), 1), 2
    ) AS cache_hit_ratio_pct
FROM pg_stat_database
WHERE datname = current_database()
"""

_SEQ_VS_IDX_SCANS = """
SELECT
    relname AS table_name,
    seq_scan,
    idx_scan,
    seq_tup_read,
    CASE WHEN seq_scan + idx_scan = 0 THEN 0
         ELSE round(100.0 * seq_scan / (seq_scan + idx_scan), 1)
    END AS seq_scan_pct
FROM pg_stat_user_tables
ORDER BY seq_scan DESC
LIMIT 20
"""

_LONG_RUNNING = """
SELECT
    pid,
    now() - query_start AS running_for,
    state,
    left(query, 120) AS query_preview
FROM pg_stat_activity
WHERE datname = current_database()
  AND state = 'active'
  AND query_start < now() - interval '2 seconds'
ORDER BY query_start
"""


def main() -> None:
    settings = get_settings()
    if not settings.has_database:
        raise SystemExit("DATABASE_URL is not set.")
    engine = create_engine(settings.sqlalchemy_url)
    try:
        with engine.connect() as conn:
            print("=== Connections by state ===")
            for row in conn.execute(text(_ACTIVE_CONNECTIONS)):
                print(f"  {row.state or '(none)':<20} {row.count}")

            print("\n=== Cache hit ratio (this database) ===")
            row = conn.execute(text(_CACHE_HIT_RATIO)).one()
            print(f"  blocks_hit={row.blocks_hit} blocks_read={row.blocks_read} "
                  f"hit_ratio={row.cache_hit_ratio_pct}%")
            if row.cache_hit_ratio_pct is not None and row.cache_hit_ratio_pct < 90:
                print("  NOTE: below the ~99% usually expected for an OLTP workload — "
                      "likely cold cache (small/shared-RAM VPS) or large unbounded scans (see P0-2 in PERFORMANCE_AUDIT.md).")

            print("\n=== Sequential vs index scans per table (top 20 by seq_scan) ===")
            print(f"  {'table':<30} {'seq_scan':>10} {'idx_scan':>10} {'seq_tup_read':>14} {'seq_scan_%':>10}")
            for row in conn.execute(text(_SEQ_VS_IDX_SCANS)):
                print(f"  {row.table_name:<30} {row.seq_scan:>10} {row.idx_scan:>10} "
                      f"{row.seq_tup_read:>14} {row.seq_scan_pct:>9}%")

            print("\n=== Long-running queries (active > 2s right now) ===")
            rows = list(conn.execute(text(_LONG_RUNNING)))
            if not rows:
                print("  (none at the moment of this snapshot)")
            for row in rows:
                print(f"  pid={row.pid} running_for={row.running_for} state={row.state} query={row.query_preview!r}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
