from mysql_connector import open_ssh_tunnel, get_connection, close_ssh_tunnel


def get_company_ids(cursor):
    cursor.execute("""
        SELECT DISTINCT company_id
        FROM balance_points_report_sync_tracker
        ORDER BY company_id
    """)
    rows = cursor.fetchall()
    company_ids = [row['company_id'] for row in rows]
    print(f"  Found {len(company_ids)} companies to process: {company_ids}")
    return company_ids


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

    # Single query: calculate and update in one round trip
    cursor.execute("""
        UPDATE balance_points_report_summary bpr
        INNER JOIN (
            SELECT r.user_id, r.country_id, SUM(r.points) AS balance
            FROM rewardpoints r
            INNER JOIN users u ON r.user_id = u.id
            WHERE u.company_id = %s
            GROUP BY r.user_id, r.country_id
        ) calc ON calc.user_id = bpr.vc_user_id
              AND calc.country_id = bpr.country_id
        SET bpr.current_balance_points = calc.balance
        WHERE bpr.company_id = %s
    """, (company_id, company_id))

    conn.commit()
    print(f"  Refreshed current_balance_points for {cursor.rowcount} rows")


def refresh_expected_balance_and_mismatch(cursor, conn, company_id):
    print("\n  Refreshing expected_balance and data_mismatch...")
    cursor.execute("""
        UPDATE balance_points_report_summary
        SET
            expected_balance = (
                points_allocated + locked_points +
                cashback_points + purchased_points -
                gv_redeemed_points - amazon_redeemed_points -
                merchandise_redeemed_points - experience_redeemed_points
            ),
            data_mismatch = CASE
                WHEN (
                    points_allocated + locked_points +
                    cashback_points + purchased_points -
                    gv_redeemed_points - amazon_redeemed_points -
                    merchandise_redeemed_points - experience_redeemed_points
                ) = current_balance_points THEN 0 ELSE 1 END
        WHERE company_id = %s
    """, (company_id,))
    conn.commit()
    print(f"  Updated expected_balance and data_mismatch for {cursor.rowcount} rows")


def detect_and_insert_new_users(cursor, conn, company_id):
    print("\n  Detecting new users...")
    cursor.execute("""
        SELECT DISTINCT u.id AS user_id, ep.country_id
        FROM users u
        INNER JOIN employee_points_transactions ept ON ept.userid = u.id
        INNER JOIN employee_points ep ON ept.employee_points_id = ep.id
            AND ep.status IN (1, 4)
            AND ep.amount > 0
        WHERE u.company_id = %s
        AND NOT EXISTS (
            SELECT 1 FROM balance_points_report_summary bpr
            WHERE bpr.vc_user_id = u.id
            AND bpr.country_id = ep.country_id
        )
    """, (company_id,))
    new_rows = cursor.fetchall()

    if not new_rows:
        print("  No new users detected")
        return 0

    for row in new_rows:
        cursor.execute("""
            INSERT IGNORE INTO balance_points_report_summary
                (vc_user_id, company_id, country_id, country_name, status,
                 points_allocated, locked_points, cashback_points,
                 gv_redeemed_points, amazon_redeemed_points,
                 merchandise_redeemed_points, experience_redeemed_points,
                 purchased_points, current_balance_points,
                 historical_migration_locked_points, normal_locked_points,
                 expected_balance, data_mismatch)
            SELECT
                u.id, u.company_id, c.id, c.country_name,
                (CASE WHEN u.status = 1 THEN 'Active' ELSE 'Inactive' END),
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
            FROM users u
            INNER JOIN countries c ON c.id = %s
            WHERE u.id = %s
        """, (row['country_id'], row['user_id']))
    conn.commit()

    new_user_ids = sorted({row['user_id'] for row in new_rows})
    placeholders = ", ".join(str(uid) for uid in new_user_ids)
    cursor.execute("""
        SELECT
            ept.userid,
            decrypt(ed.employee_id) AS employee_id,
            decrypt(ed.name) AS employee_name,
            decrypt(ed.email_id) AS employee_email
        FROM employee_details ed
        INNER JOIN employee_points ep ON ed.email_id = ep.employee_id
        INNER JOIN employee_points_transactions ept ON ep.id = ept.employee_points_id
        WHERE ed.company_id = %s
        AND ept.userid IN ({new_user_ids})
        ORDER BY ed.id DESC
    """.format(new_user_ids=placeholders), (company_id,))
    emp_rows = cursor.fetchall()

    emp_lookup = {}
    for row in emp_rows:
        uid = row['userid']
        if uid not in emp_lookup:
            emp_lookup[uid] = row

    for uid, data in emp_lookup.items():
        cursor.execute("""
            UPDATE balance_points_report_summary
            SET employee_id = %s,
                employee_name = %s,
                employee_email = %s
            WHERE vc_user_id = %s
        """, (data['employee_id'], data['employee_name'],
              data['employee_email'], uid))
    conn.commit()

    print(f"  Detected and inserted {len(new_rows)} new user-country rows "
          f"({len(new_user_ids)} users)")
    return len(new_rows)


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
        print("Detecting and inserting new users...")
        new_users = detect_and_insert_new_users(cursor, conn, company_id)
        summary['new_users_inserted'] = new_users
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
        refresh_expected_balance_and_mismatch(cursor, conn, company_id)
        print(f"\n  Summary for company {company_id}:")
        for table, count in summary.items():
            print(f"    {table}: {count} pairs affected")
    finally:
        cursor.close()
        conn.close()


def run():
    open_ssh_tunnel()
    try:
        # Open one connection just to fetch company IDs
        temp_conn = get_connection()
        temp_cursor = temp_conn.cursor(dictionary=True)
        try:
            company_ids = get_company_ids(temp_cursor)
        finally:
            temp_cursor.close()
            temp_conn.close()

        for company_id in company_ids:
            print(f"\n{'='*50}")
            print(f"Processing company_id: {company_id}")
            print(f"{'='*50}")
            run_for_company(company_id)
        print("\n✅ Incremental sync complete.")
    finally:
        close_ssh_tunnel()


if __name__ == '__main__':
    run()
