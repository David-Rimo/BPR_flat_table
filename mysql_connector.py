import mysql.connector
import os
from sshtunnel import SSHTunnelForwarder

# SSH Config
SSH_HOST = 'uat1.vantagecircle.com'
SSH_PORT = 22
SSH_USER = 'vantage'
SSH_KEY  = os.path.expanduser('~/.ssh/id_rsa')

# DB Config
SQL_HOSTNAME = 'private-vc-uat-mysql-do-user-384774-0.b.db.ondigitalocean.com'
SQL_PORT     = 25060
SQL_USER     = 'tdp_user'
SQL_PWD      = 'tdp_passwd123'
SQL_DB       = 'thedealspoint'

tunnel = None

def open_ssh_tunnel():
    global tunnel
    tunnel = SSHTunnelForwarder(
        (SSH_HOST, SSH_PORT),
        ssh_username=SSH_USER,
        ssh_pkey=SSH_KEY,
        remote_bind_address=(SQL_HOSTNAME, SQL_PORT)
    )
    tunnel.start()
    print(f"✅ SSH Tunnel opened on local port {tunnel.local_bind_port}")

def get_connection():
    conn = mysql.connector.connect(
        host='127.0.0.1',
        port=tunnel.local_bind_port,
        user=SQL_USER,
        password=SQL_PWD,
        database=SQL_DB,
        ssl_disabled=False,
        use_pure=True,
        connection_timeout=300
    )
    return conn

def close_ssh_tunnel():
    global tunnel
    if tunnel is not None:
        tunnel.stop()
        print("✅ Tunnel closed cleanly")

if __name__ == '__main__':
    try:
        print("Opening SSH tunnel...")
        open_ssh_tunnel()
        print("Connecting to DB...")
        conn = get_connection()
        if conn.is_connected():
            print("✅ DB Connected successfully!")
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT COUNT(*) as count FROM balance_points_report_summary")
            result = cursor.fetchone()
            print(f"✅ Flat table row count: {result['count']}")
            cursor.close()
            conn.close()
        close_ssh_tunnel()
    except Exception as e:
        print(f"❌ Error: {e}")
