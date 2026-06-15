from mysql_connector import open_ssh_tunnel, get_connection, close_ssh_tunnel

COMPANY_IDS = [63]


def get_last_seen_id(cursor, company_id, table_name):
    cursor.execute(
        "SELECT last_seen_id FROM balance_points_report_sync_tracker "
        "WHERE company_id = %s AND source_table = %s",
        (company_id, table_name)
    )
    row = cursor.fetchone()
    return row['last_seen_id'] if row else 0


def update_tracker(cursor, conn, company_id, table_name, new_max_id):
    cursor.execute(
        """
        INSERT INTO balance_points_report_sync_tracker
            (company_id, source_table, last_seen_id, last_synced_at)
        VALUES (%s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            last_seen_id = VALUES(last_seen_id),
            last_synced_at = NOW()
        """,
        (company_id, table_name, new_max_id)
    )
    conn.commit()


def apply_delta(cursor, user_id, country_id, column_name, delta):
    if delta == 0:
        return
    cursor.execute(
        f"UPDATE balance_points_report_summary "
        f"SET {column_name} = {column_name} + %s "
        f"WHERE vc_user_id = %s AND country_id = %s",
        (delta, user_id, country_id)
    )


def process_employee_points(cursor, conn, company_id):
    table = 'employee_points_id'
    last_id = get_last_seen_id(cursor, company_id, table)
    cursor.execute("""
        SELECT ept.userid, ep.country_id,
               SUM(ept.points) AS delta,
               MAX(ept.employee_points_id) AS max_id
        FROM employee_points_transactions ept
        INNER JOIN employee_points ep ON ept.employee_points_id = ep.id
        INNER JOIN users u ON ept.userid = u.id
        WHERE ept.employee_points_id > %s
        AND u.company_id = %s
        AND ep.status IN (1, 4)
        AND ep.amount > 0
        GROUP BY ept.userid, ep.country_id
    """, (last_id, company_id))
    rows = cursor.fetchall()
    new_max_id = last_id
    for row in rows:
        apply_delta(cursor, row['userid'], row['country_id'], 'points_allocated', row['delta'])
        new_max_id = max(new_max_id, row['max_id'])
    update_tracker(cursor, conn, company_id, table, new_max_id)
    conn.commit()
    print(f"  employee_points_id: {len(rows)} user-country pairs affected")
    return len(rows)


def process_locked_points(cursor, conn, company_id):
    table = 'locked_points_transactions'
    last_id = get_last_seen_id(cursor, company_id, table)
    cursor.execute("""
        SELECT
            lpt.userid, lpt.country_id,
            SUM(lpt.points) AS total_delta,
            COALESCE(SUM(CASE WHEN lpt.unlock_points_purpose
                LIKE '%%historical%%' THEN lpt.points END), 0) AS hist_delta,
            COALESCE(SUM(CASE WHEN lpt.unlock_points_purpose
                NOT LIKE '%%historical%%' THEN lpt.points END), 0) AS normal_delta,
            MAX(lpt.id) AS max_id
        FROM locked_points_transactions lpt
        INNER JOIN users u ON lpt.userid = u.id
        WHERE lpt.id > %s AND u.company_id = %s
        GROUP BY lpt.userid, lpt.country_id
    """, (last_id, company_id))
    rows = cursor.fetchall()
    new_max_id = last_id
    for row in rows:
        apply_delta(cursor, row['userid'], row['country_id'], 'locked_points', row['total_delta'])
        apply_delta(cursor, row['userid'], row['country_id'], 'historical_migration_locked_points', row['hist_delta'])
        apply_delta(cursor, row['userid'], row['country_id'], 'normal_locked_points', row['normal_delta'])
        new_max_id = max(new_max_id, row['max_id'])
    update_tracker(cursor, conn, company_id, table, new_max_id)
    conn.commit()
    print(f"  locked_points_transactions: {len(rows)} user-country pairs affected")
    return len(rows)


def process_affiliate_reward(cursor, conn, company_id):
    table = 'affiliate_reward_point'
    last_id = get_last_seen_id(cursor, company_id, table)
    cursor.execute("""
        SELECT arp.user_id, SUM(arp.points) AS delta, MAX(arp.id) AS max_id
        FROM affiliate_reward_point arp
        INNER JOIN users u ON arp.user_id = u.id
        WHERE arp.id > %s AND u.company_id = %s
        GROUP BY arp.user_id
    """, (last_id, company_id))
    rows = cursor.fetchall()
    new_max_id = last_id
    for row in rows:
        apply_delta(cursor, row['user_id'], 91, 'cashback_points', row['delta'])
        cursor.execute("SELECT ROW_COUNT() AS affected")
        result = cursor.fetchone()
        if result['affected'] == 0:
            print(f"  WARNING: cashback delta skipped — no flat table row for "
                  f"user_id={row['user_id']} country_id=91")
        new_max_id = max(new_max_id, row['max_id'])
    update_tracker(cursor, conn, company_id, table, new_max_id)
    conn.commit()
    print(f"  affiliate_reward_point: {len(rows)} user-country pairs affected")
    return len(rows)


def process_gift_voucher(cursor, conn, company_id):
    table = 'gift_voucher'
    last_id = get_last_seen_id(cursor, company_id, table)
    cursor.execute("""
        SELECT gv.users_id, gv.country_id,
               SUM(gv.fee_added_points) AS delta, MAX(gv.id) AS max_id
        FROM gift_voucher gv
        INNER JOIN users u ON gv.users_id = u.id
        WHERE gv.id > %s AND gv.is_redeemed = 1
        AND gv.order_item_detail_id IS NULL AND u.company_id = %s
        GROUP BY gv.users_id, gv.country_id
    """, (last_id, company_id))
    rows = cursor.fetchall()
    new_max_id = last_id
    for row in rows:
        apply_delta(cursor, row['users_id'], row['country_id'], 'gv_redeemed_points', row['delta'])
        new_max_id = max(new_max_id, row['max_id'])
    update_tracker(cursor, conn, company_id, table, new_max_id)
    conn.commit()
    print(f"  gift_voucher: {len(rows)} user-country pairs affected")
    return len(rows)


def process_orders(cursor, conn, company_id):
    table = 'orders'
    last_id = get_last_seen_id(cursor, company_id, table)
    cursor.execute("""
        SELECT
            o.user_id, o.country_id,
            COALESCE(SUM(CASE WHEN oid.sku_id = 44959
                THEN oid.price_in_points * oid.quantity END), 0) AS amazon_delta,
            COALESCE(SUM(CASE WHEN oid.sku_id <> 44959
                THEN oid.price_in_points * oid.quantity END), 0) AS merch_delta,
            MAX(o.id) AS max_id
        FROM orders o
        INNER JOIN order_item_details oid ON o.id = oid.order_id
        INNER JOIN users u ON o.user_id = u.id
        WHERE o.id > %s AND oid.status = 1
        AND o.milestone_year IS NULL AND u.company_id = %s
        GROUP BY o.user_id, o.country_id
    """, (last_id, company_id))
    rows = cursor.fetchall()
    new_max_id = last_id
    for row in rows:
        apply_delta(cursor, row['user_id'], row['country_id'], 'amazon_redeemed_points', row['amazon_delta'])
        apply_delta(cursor, row['user_id'], row['country_id'], 'merchandise_redeemed_points', row['merch_delta'])
        new_max_id = max(new_max_id, row['max_id'])
    update_tracker(cursor, conn, company_id, table, new_max_id)
    conn.commit()
    print(f"  orders: {len(rows)} user-country pairs affected")
    return len(rows)


def process_experiences(cursor, conn, company_id):
    table = 'experiences_transactions'
    last_id = get_last_seen_id(cursor, company_id, table)
    cursor.execute("""
        SELECT et.user_id, et.country_id,
               SUM(et.redeemed_points) AS delta, MAX(et.id) AS max_id
        FROM experiences_transactions et
        INNER JOIN users u ON et.user_id = u.id
        WHERE et.id > %s AND et.booking_status = 'booked' AND u.company_id = %s
        GROUP BY et.user_id, et.country_id
    """, (last_id, company_id))
    rows = cursor.fetchall()
    new_max_id = last_id
    for row in rows:
        apply_delta(cursor, row['user_id'], row['country_id'], 'experience_redeemed_points', row['delta'])
        new_max_id = max(new_max_id, row['max_id'])
    update_tracker(cursor, conn, company_id, table, new_max_id)
    conn.commit()
    print(f"  experiences_transactions: {len(rows)} user-country pairs affected")
    return len(rows)


def process_topup(cursor, conn, company_id):
    table = 'points_topup_transaction_details'
    last_id = get_last_seen_id(cursor, company_id, table)
    cursor.execute("""
        SELECT tp.user_id, tp.country_id,
               SUM(tp.points) AS delta, MAX(tp.id) AS max_id
        FROM points_topup_transaction_details tp
        INNER JOIN users u ON tp.user_id = u.id
        WHERE tp.id > %s AND tp.status = 1 AND u.company_id = %s
        GROUP BY tp.user_id, tp.country_id
    """, (last_id, company_id))
    rows = cursor.fetchall()
    new_max_id = last_id
    for row in rows:
        apply_delta(cursor, row['user_id'], row['country_id'], 'purchased_points', row['delta'])
        new_max_id = max(new_max_id, row['max_id'])
    update_tracker(cursor, conn, company_id, table, new_max_id)
    conn.commit()
    print(f"  points_topup_transaction_details: {len(rows)} user-country pairs affected")
    return len(rows)


def refresh_current_balance(cursor, conn, company_id):
    print("\n  Refreshing current_balance_points...")
    cursor.execute("""
        SELECT r.user_id, r.country_id, SUM(r.points) AS balance
        FROM rewardpoints r
        WHERE r.user_id IN (
            SELECT id FROM users WHERE company_id = %s
        )
        GROUP BY r.user_id, r.country_id
    """, (company_id,))
    rows = cursor.fetchall()
    updated = 0
    for row in rows:
        cursor.execute("""
            UPDATE balance_points_report_summary
            SET current_balance_points = %s
            WHERE vc_user_id = %s AND country_id = %s AND company_id = %s
        """, (row['balance'], row['user_id'], row['country_id'], company_id))
        if cursor.rowcount > 0:
            updated += 1
    conn.commit()
    print(f"  Refreshed current_balance_points for {updated} rows")


def run_for_company(company_id):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SET SESSION wait_timeout = 28800")
    cursor.execute("SET SESSION interactive_timeout = 28800")
    cursor.execute("SET SESSION net_read_timeout = 3600")
    cursor.execute("SET SESSION net_write_timeout = 3600")
    cursor.execute("SET SESSION sql_mode = ''")
    summary = {}
    try:
        print("Processing employee_points_transactions...")
        summary['employee_points_id'] = process_employee_points(cursor, conn, company_id)
        print("Processing locked_points_transactions...")
        summary['locked_points_transactions'] = process_locked_points(cursor, conn, company_id)
        print("Processing affiliate_reward_point...")
        summary['affiliate_reward_point'] = process_affiliate_reward(cursor, conn, company_id)
        print("Processing gift_voucher...")
        summary['gift_voucher'] = process_gift_voucher(cursor, conn, company_id)
        print("Processing orders...")
        summary['orders'] = process_orders(cursor, conn, company_id)
        print("Processing experiences_transactions...")
        summary['experiences_transactions'] = process_experiences(cursor, conn, company_id)
        print("Processing points_topup_transaction_details...")
        summary['points_topup_transaction_details'] = process_topup(cursor, conn, company_id)
        refresh_current_balance(cursor, conn, company_id)
        print(f"\n  Summary for company {company_id}:")
        for table, count in summary.items():
            print(f"    {table}: {count} pairs affected")
    finally:
        cursor.close()
        conn.close()


def run():
    open_ssh_tunnel()
    try:
        for company_id in COMPANY_IDS:
            print(f"\n{'='*50}")
            print(f"Processing company_id: {company_id}")
            print(f"{'='*50}")
            run_for_company(company_id)
        print("\n✅ Incremental sync complete.")
    finally:
        close_ssh_tunnel()


if __name__ == '__main__':
    run()
