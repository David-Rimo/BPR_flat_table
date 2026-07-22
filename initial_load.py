import math
import argparse
from mysql_connector import open_ssh_tunnel, get_connection, close_ssh_tunnel

BATCH_SIZE = 200

TRACKED_TABLES = [
    'locked_points_transactions',
    'affiliate_reward_point',
    'gift_voucher',
    'orders',
    'experiences_transactions',
    'points_topup_transaction_details',
]

MAIN_QUERY = """
WITH
emp_points AS (
    SELECT ept.userid, ep.country_id, SUM(ept.points) AS points_allocated
    FROM employee_points_transactions ept
    INNER JOIN employee_points ep ON ept.employee_points_id = ep.id
    WHERE ep.company_id = %(company_id)s
    AND ep.status IN (1, 4)
    AND ep.amount > 0
    AND ept.userid IN ({user_ids})
    GROUP BY ept.userid, ep.country_id
),
locked_pts AS (
    SELECT lpt.userid, lpt.country_id, SUM(lpt.points) AS locked_points
    FROM locked_points_transactions lpt
    WHERE lpt.userid IN ({user_ids})
    GROUP BY lpt.userid, lpt.country_id
),
locked_hist AS (
    SELECT
        lpt.userid,
        lpt.country_id,
        COALESCE(SUM(CASE WHEN lpt.unlock_points_purpose LIKE '%historical%'
            THEN lpt.points END), 0) AS historical_migration_locked_points,
        COALESCE(SUM(CASE WHEN lpt.unlock_points_purpose NOT LIKE '%historical%'
            THEN lpt.points END), 0) AS normal_locked_points
    FROM locked_points_transactions lpt
    WHERE lpt.userid IN ({user_ids})
    GROUP BY lpt.userid, lpt.country_id
),
cashback_pts AS (
    SELECT arp.user_id, SUM(arp.points) AS cashback_points
    FROM affiliate_reward_point arp
    WHERE arp.user_id IN ({user_ids})
    GROUP BY arp.user_id
),
gv_pts AS (
    SELECT gv.users_id, gv.country_id, SUM(gv.fee_added_points) AS gv_redeemed_points
    FROM gift_voucher gv
    WHERE gv.is_redeemed = 1
    AND gv.order_item_detail_id IS NULL
    AND gv.users_id IN ({user_ids})
    GROUP BY gv.users_id, gv.country_id
),
amazon_pts AS (
    SELECT o.user_id, o.country_id,
           SUM(oid.price_in_points * oid.quantity) AS amazon_redeemed_points
    FROM orders o
    INNER JOIN order_item_details oid ON o.id = oid.order_id
    WHERE oid.sku_id = 44959
    AND oid.status = 1
    AND o.milestone_year IS NULL
    AND o.user_id IN ({user_ids})
    GROUP BY o.user_id, o.country_id
),
merch_pts AS (
    SELECT o.user_id, o.country_id,
           SUM(oid.price_in_points * oid.quantity) AS merchandise_redeemed_points
    FROM orders o
    INNER JOIN order_item_details oid ON o.id = oid.order_id
    WHERE oid.sku_id <> 44959
    AND oid.status = 1
    AND o.milestone_year IS NULL
    AND o.user_id IN ({user_ids})
    GROUP BY o.user_id, o.country_id
),
exp_pts AS (
    SELECT et.user_id, et.country_id,
           SUM(et.redeemed_points) AS experience_redeemed_points
    FROM experiences_transactions et
    WHERE et.booking_status = 'booked'
    AND et.user_id IN ({user_ids})
    GROUP BY et.user_id, et.country_id
),
topup_pts AS (
    SELECT tp.user_id, tp.country_id, SUM(tp.points) AS purchased_points
    FROM points_topup_transaction_details tp
    WHERE tp.status = 1
    AND tp.user_id IN ({user_ids})
    GROUP BY tp.user_id, tp.country_id
),
balance_pts AS (
    SELECT r.user_id, r.country_id, SUM(r.points) AS current_balance_points
    FROM rewardpoints r
    WHERE r.user_id IN ({user_ids})
    GROUP BY r.user_id, r.country_id
),
last_login AS (
    SELECT ulh.userid, MAX(ulh.logged_on) AS last_login_at
    FROM user_login_history ulh
    WHERE ulh.userid IN ({user_ids})
    GROUP BY ulh.userid
)
SELECT
    u.id AS user_id,
    u.company_id,
    (CASE WHEN u.status = 1 THEN 'Active' ELSE 'Inactive' END) AS status,
    NULL AS employee_id,
    NULL AS employee_name,
    NULL AS employee_email,
    rp.country_id,
    c.country_name,
    COALESCE(ep_agg.points_allocated, 0) AS points_allocated,
    COALESCE(lp.locked_points, 0) AS locked_points,
    COALESCE(CASE WHEN rp.country_id = 91 THEN cb.cashback_points ELSE 0 END, 0) AS cashback_points,
    COALESCE(gv.gv_redeemed_points, 0) AS gv_redeemed_points,
    COALESCE(az.amazon_redeemed_points, 0) AS amazon_redeemed_points,
    COALESCE(me.merchandise_redeemed_points, 0) AS merchandise_redeemed_points,
    COALESCE(ex.experience_redeemed_points, 0) AS experience_redeemed_points,
    COALESCE(tp.purchased_points, 0) AS purchased_points,
    COALESCE(bp.current_balance_points, 0) AS current_balance_points,
    (
        COALESCE(ep_agg.points_allocated, 0) +
        COALESCE(lp.locked_points, 0) +
        COALESCE(CASE WHEN rp.country_id = 91 THEN cb.cashback_points ELSE 0 END, 0) +
        COALESCE(tp.purchased_points, 0) -
        COALESCE(gv.gv_redeemed_points, 0) -
        COALESCE(az.amazon_redeemed_points, 0) -
        COALESCE(me.merchandise_redeemed_points, 0) -
        COALESCE(ex.experience_redeemed_points, 0)
    ) AS expected_balance,
    CASE WHEN (
        COALESCE(ep_agg.points_allocated, 0) +
        COALESCE(lp.locked_points, 0) +
        COALESCE(CASE WHEN rp.country_id = 91 THEN cb.cashback_points ELSE 0 END, 0) +
        COALESCE(tp.purchased_points, 0) -
        COALESCE(gv.gv_redeemed_points, 0) -
        COALESCE(az.amazon_redeemed_points, 0) -
        COALESCE(me.merchandise_redeemed_points, 0) -
        COALESCE(ex.experience_redeemed_points, 0)
    ) = COALESCE(bp.current_balance_points, 0) THEN 0 ELSE 1 END AS data_mismatch,
    COALESCE(lh.historical_migration_locked_points, 0) AS historical_migration_locked_points,
    COALESCE(lh.normal_locked_points, 0) AS normal_locked_points,
    ll.last_login_at,
    u.created_at AS user_created_at
FROM rewardpoints rp
INNER JOIN users u ON rp.user_id = u.id
INNER JOIN countries c ON c.id = rp.country_id
LEFT JOIN emp_points ep_agg ON ep_agg.userid = u.id
    AND ep_agg.country_id = rp.country_id
LEFT JOIN locked_pts lp ON lp.userid = u.id
    AND lp.country_id = rp.country_id
LEFT JOIN locked_hist lh ON lh.userid = u.id
    AND lh.country_id = rp.country_id
LEFT JOIN cashback_pts cb ON cb.user_id = u.id
LEFT JOIN gv_pts gv ON gv.users_id = u.id
    AND gv.country_id = rp.country_id
LEFT JOIN amazon_pts az ON az.user_id = u.id
    AND az.country_id = rp.country_id
LEFT JOIN merch_pts me ON me.user_id = u.id
    AND me.country_id = rp.country_id
LEFT JOIN exp_pts ex ON ex.user_id = u.id
    AND ex.country_id = rp.country_id
LEFT JOIN topup_pts tp ON tp.user_id = u.id
    AND tp.country_id = rp.country_id
LEFT JOIN balance_pts bp ON bp.user_id = u.id
    AND bp.country_id = rp.country_id
LEFT JOIN last_login ll ON ll.userid = u.id
WHERE u.company_id = %(company_id)s
AND u.id IN ({user_ids})
GROUP BY u.id, rp.country_id
"""

EMP_DETAILS_QUERY = """
SELECT
    ept.userid,
    decrypt(ed.employee_id) AS employee_id,
    decrypt(ed.name) AS employee_name,
    decrypt(ed.email_id) AS employee_email
FROM employee_details ed
INNER JOIN employee_points ep ON ed.email_id = ep.employee_id
INNER JOIN employee_points_transactions ept ON ep.id = ept.employee_points_id
WHERE ed.company_id = %(company_id)s
AND ept.userid IN ({user_ids})
ORDER BY ed.id DESC
"""

INSERT_SQL = """
INSERT INTO balance_points_report_summary (
    user_id, company_id, status,
    employee_id, employee_name, employee_email,
    country_id, country_name,
    points_allocated, locked_points, cashback_points,
    gv_redeemed_points, amazon_redeemed_points, merchandise_redeemed_points,
    experience_redeemed_points, purchased_points, current_balance_points,
    expected_balance, data_mismatch,
    historical_migration_locked_points, normal_locked_points,
    last_login_at, user_created_at
) VALUES (
    %(user_id)s, %(company_id)s, %(status)s,
    %(employee_id)s, %(employee_name)s, %(employee_email)s,
    %(country_id)s, %(country_name)s,
    %(points_allocated)s, %(locked_points)s, %(cashback_points)s,
    %(gv_redeemed_points)s, %(amazon_redeemed_points)s, %(merchandise_redeemed_points)s,
    %(experience_redeemed_points)s, %(purchased_points)s, %(current_balance_points)s,
    %(expected_balance)s, %(data_mismatch)s,
    %(historical_migration_locked_points)s, %(normal_locked_points)s,
    %(last_login_at)s, %(user_created_at)s
)
ON DUPLICATE KEY UPDATE
    company_id = VALUES(company_id),
    status = VALUES(status),
    employee_id = VALUES(employee_id),
    employee_name = VALUES(employee_name),
    employee_email = VALUES(employee_email),
    country_name = VALUES(country_name),
    points_allocated = VALUES(points_allocated),
    locked_points = VALUES(locked_points),
    cashback_points = VALUES(cashback_points),
    gv_redeemed_points = VALUES(gv_redeemed_points),
    amazon_redeemed_points = VALUES(amazon_redeemed_points),
    merchandise_redeemed_points = VALUES(merchandise_redeemed_points),
    experience_redeemed_points = VALUES(experience_redeemed_points),
    purchased_points = VALUES(purchased_points),
    current_balance_points = VALUES(current_balance_points),
    expected_balance = VALUES(expected_balance),
    data_mismatch = VALUES(data_mismatch),
    historical_migration_locked_points = VALUES(historical_migration_locked_points),
    normal_locked_points = VALUES(normal_locked_points),
    last_login_at = VALUES(last_login_at),
    user_created_at = VALUES(user_created_at)
"""

UPDATE_EMP_SQL = """
UPDATE balance_points_report_summary
SET employee_id = %(employee_id)s,
    employee_name = %(employee_name)s,
    employee_email = %(employee_email)s
WHERE user_id = %(userid)s
"""


def get_all_user_ids(cursor, company_id):
    cursor.execute(
        "SELECT DISTINCT u.id AS user_id "
        "FROM users u "
        "INNER JOIN rewardpoints rp ON rp.user_id = u.id "
        "INNER JOIN employee_points_transactions ept ON ept.userid = u.id "
        "INNER JOIN employee_points ep ON ept.employee_points_id = ep.id "
        "    AND ep.country_id = rp.country_id "
        "WHERE u.company_id = %s "
        "AND ep.status IN (1, 4) "
        "AND ep.amount > 0 "
        "AND ep.company_id = %s",
        (company_id, company_id)
    )
    return [row['user_id'] for row in cursor.fetchall()]


def snapshot_max_ids(cursor, company_id):
    print("=== Phase 3 Step 1: Snapshotting MAX IDs ===")
    snapshots = {}
    for table in TRACKED_TABLES:
        cursor.execute(f"SELECT COALESCE(MAX(id), 0) AS max_id FROM {table}")
        snapshots[table] = cursor.fetchone()['max_id']
        print(f"  {table}: max_id = {snapshots[table]}")
    cursor.execute("""
        SELECT COALESCE(MAX(ept.employee_points_id), 0) AS max_id
        FROM employee_points_transactions ept
        INNER JOIN employee_points ep ON ep.id = ept.employee_points_id
        WHERE ep.company_id = %s
        AND ep.status IN (1, 4)
        AND ep.amount > 0
    """, (company_id,))
    snapshots['employee_points_id'] = cursor.fetchone()['max_id']
    print(f"  employee_points_id: max_id = {snapshots['employee_points_id']}")
    return snapshots


def phase1_insert(cursor, conn, user_ids, company_id):
    total_batches = math.ceil(len(user_ids) / BATCH_SIZE)
    total_inserted = 0
    failed_batches = []
    for batch_num, start in enumerate(range(0, len(user_ids), BATCH_SIZE), 1):
        batch = user_ids[start:start + BATCH_SIZE]
        try:
            placeholders = ", ".join(str(uid) for uid in batch)
            query = MAIN_QUERY.format(user_ids=placeholders)
            cursor.execute(query, {'company_id': company_id})
            rows = cursor.fetchall()
            if rows:
                cursor.executemany(INSERT_SQL, rows)
                conn.commit()
            total_inserted += len(rows)
            print(f"Phase 1 Batch {batch_num}/{total_batches}: inserted {len(rows)} rows "
                  f"(total so far: {total_inserted})")
        except Exception as e:
            conn.rollback()
            print(f"  ❌ Phase 1 Batch {batch_num} FAILED: {e} — skipping batch")
            failed_batches.append(batch_num)
    if failed_batches:
        print(f"  ⚠️ {len(failed_batches)} batches failed: {failed_batches}")
    return total_inserted


def phase2_update(cursor, conn, user_ids, company_id):
    total_batches = math.ceil(len(user_ids) / BATCH_SIZE)
    total_updated = 0
    for batch_num, start in enumerate(range(0, len(user_ids), BATCH_SIZE), 1):
        batch = user_ids[start:start + BATCH_SIZE]
        try:
            placeholders = ", ".join(str(uid) for uid in batch)
            query = EMP_DETAILS_QUERY.format(user_ids=placeholders)
            cursor.execute(query, {'company_id': company_id})
            emp_rows = cursor.fetchall()
            emp_lookup = {}
            for row in emp_rows:
                uid = row['userid']
                if uid not in emp_lookup:
                    emp_lookup[uid] = row
            batch_updated = 0
            for uid in batch:
                if uid not in emp_lookup:
                    continue
                data = emp_lookup[uid]
                cursor.execute(UPDATE_EMP_SQL, {
                    'employee_id': data['employee_id'],
                    'employee_name': data['employee_name'],
                    'employee_email': data['employee_email'],
                    'userid': uid,
                })
                batch_updated += cursor.rowcount
            conn.commit()
            total_updated += batch_updated
            print(f"Phase 2 Batch {batch_num}/{total_batches}: updated {batch_updated} rows "
                  f"(total so far: {total_updated})")
        except Exception as e:
            conn.rollback()
            print(f"  ❌ Phase 2 Batch {batch_num} FAILED: {e} — skipping batch")
    return total_updated


def update_sync_tracker(cursor, conn, snapshots, company_id):
    print("\n=== Phase 3 Step 2: Updating balance_points_report_sync_tracker ===")
    for table, max_id in snapshots.items():
        cursor.execute(
            """
            INSERT INTO balance_points_report_sync_tracker
                (company_id, source_table, last_seen_id, last_synced_at)
            VALUES (%s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                last_seen_id = VALUES(last_seen_id),
                last_synced_at = NOW()
            """,
            (company_id, table, max_id)
        )
        print(f"  {table}: last_seen_id = {max_id}")
    conn.commit()


def run(company_id, phase2only=False):
    open_ssh_tunnel()
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
        close_ssh_tunnel()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('company_id', type=int, help='Company ID to load')
    parser.add_argument('--phase2only', action='store_true')
    args = parser.parse_args()
    run(args.company_id, args.phase2only)
