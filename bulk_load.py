import math
import sys

from mysql_connector import open_ssh_tunnel, get_connection, close_ssh_tunnel
from initial_load import (
    BATCH_SIZE,
    snapshot_max_ids,
    get_all_user_ids,
    phase1_insert,
    phase2_update,
    update_sync_tracker,
)

# Fill in the company IDs to bulk load below.
COMPANY_IDS = [443, 441, 1004, 355]


def load_company(company_id, phase2only=False):
    """Run the initial load for a single company using the already-open tunnel.

    Mirrors initial_load.run() but does NOT open/close the SSH tunnel — the
    tunnel is opened once by the bulk driver and reused across companies.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SET SESSION wait_timeout = 28800")
    cursor.execute("SET SESSION interactive_timeout = 28800")
    cursor.execute("SET SESSION net_read_timeout = 3600")
    cursor.execute("SET SESSION net_write_timeout = 3600")
    cursor.execute("SET SESSION sql_mode = ''")
    try:
        try:
            snapshots = snapshot_max_ids(cursor, company_id)
        except Exception as e:
            print(f"❌ Failed to snapshot MAX IDs: {e}")
            return

        try:
            print(f"\nFetching user IDs for company {company_id}...")
            user_ids = get_all_user_ids(cursor, company_id)
            total_batches = math.ceil(len(user_ids) / BATCH_SIZE)
            print(f"Found {len(user_ids)} users across {total_batches} batches")
        except Exception as e:
            print(f"❌ Failed to fetch user IDs: {e}")
            return

        if not phase2only:
            try:
                print("\n=== Phase 1: Main data insert ===")
                inserted = phase1_insert(cursor, conn, user_ids, company_id)
                print(f"Phase 1 complete: {inserted} rows inserted")
            except Exception as e:
                print(f"❌ Phase 1 failed: {e}")
                conn.rollback()
                print("Continuing to Phase 2...")

        try:
            print("\n=== Phase 2: Employee details update ===")
            updated = phase2_update(cursor, conn, user_ids, company_id)
            print(f"Phase 2 complete: {updated} rows updated")
        except Exception as e:
            print(f"❌ Phase 2 failed: {e}")
            conn.rollback()

        try:
            print("\n=== Phase 3: Sync tracker snapshot ===")
            update_sync_tracker(cursor, conn, snapshots, company_id)
            print("\n✅ Initial load complete.")
        except Exception as e:
            print(f"❌ Phase 3 failed — sync tracker not updated: {e}")
    finally:
        cursor.close()
        conn.close()


def get_already_loaded(cursor):
    cursor.execute(
        "SELECT DISTINCT company_id FROM balance_points_report_sync_tracker"
    )
    return {row['company_id'] for row in cursor.fetchall()}


def main():
    if not COMPANY_IDS:
        print("❌ COMPANY_IDS list is empty. Add company IDs before running.")
        sys.exit(1)

    open_ssh_tunnel()
    try:
        # Skip companies already present in the sync tracker.
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            already_loaded = get_already_loaded(cursor)
        finally:
            cursor.close()
            conn.close()

        remaining = []
        for company_id in COMPANY_IDS:
            if company_id in already_loaded:
                print(f"Skipping company {company_id} — already loaded")
            else:
                remaining.append(company_id)

        failed = []
        for i, company_id in enumerate(remaining, 1):
            print(f"\n{'='*50}")
            print(f"Loading company {i} of {len(remaining)}: company_id = {company_id}")
            print(f"{'='*50}")
            try:
                load_company(company_id)
            except Exception as e:
                print(f"❌ company_id {company_id} FAILED: {e}")
                print("Continuing to next company...")
                failed.append(company_id)

        print(f"\n✅ Bulk load complete.")
        print(f"  Loaded: {len(remaining) - len(failed)} companies")
        print(f"  Failed: {len(failed)} companies")
        if failed:
            print(f"  Failed company IDs: {failed}")
    finally:
        close_ssh_tunnel()


if __name__ == '__main__':
    main()
