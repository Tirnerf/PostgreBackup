import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.environ.get('BACKUP_DB_PATH', 'data/backup.db')
BACKUP_DIR = os.environ.get('BACKUP_DIR', 'backups')

APP_USERNAME = os.environ.get('APP_USERNAME', 'admin')
APP_PASSWORD_HASH = generate_password_hash(os.environ.get('APP_PASSWORD', 'changeme123'))


def init_db():
    os.makedirs('data', exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS backup_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id   INTEGER,
            server_name TEXT,
            database    TEXT,
            started_at  TEXT,
            finished_at TEXT,
            status      TEXT,
            message     TEXT,
            file_path   TEXT,
            file_size   INTEGER
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS servers (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT UNIQUE NOT NULL,
            ssh_host         TEXT DEFAULT '',
            ssh_user         TEXT DEFAULT '',
            ssh_key          TEXT DEFAULT '',
            ssh_password     TEXT DEFAULT '',
            docker_container TEXT DEFAULT '',
            pg_user          TEXT DEFAULT 'postgres',
            pg_password      TEXT DEFAULT ''
        )
    ''')

    # Migrate old backup_logs columns
    for col, col_type in (('server_id', 'INTEGER'), ('server_name', 'TEXT')):
        try:
            c.execute(f'ALTER TABLE backup_logs ADD COLUMN {col} {col_type}')
        except Exception:
            pass

    # Migrate servers table: add ssh_password if missing
    try:
        c.execute("ALTER TABLE servers ADD COLUMN ssh_password TEXT DEFAULT ''")
    except Exception:
        pass

    # Auto-migrate old SSH config → first server entry
    c.execute('SELECT COUNT(*) FROM servers')
    if c.fetchone()[0] == 0:
        c.execute("SELECT key, value FROM config WHERE key IN "
                  "('ssh_host','ssh_user','ssh_key','docker_container','pg_user','pg_password')")
        old = dict(c.fetchall())
        if old.get('ssh_host'):
            c.execute(
                '''INSERT OR IGNORE INTO servers
                   (name, ssh_host, ssh_user, ssh_key, docker_container, pg_user, pg_password)
                   VALUES (?,?,?,?,?,?,?)''',
                ('Kaynak Sunucu', old.get('ssh_host', ''), old.get('ssh_user', ''),
                 old.get('ssh_key', ''), old.get('docker_container', 'postgres'),
                 old.get('pg_user', 'postgres'), old.get('pg_password', ''))
            )
            # Migrate target server if configured
            c.execute("SELECT key, value FROM config WHERE key IN "
                      "('target_pg_type','target_pg_user','target_pg_password','target_docker_container')")
            tgt = dict(c.fetchall())
            tgt_type = tgt.get('target_pg_type', 'none')
            if tgt_type == 'docker':
                c.execute(
                    '''INSERT OR IGNORE INTO servers
                       (name, ssh_host, ssh_user, ssh_key, docker_container, pg_user, pg_password)
                       VALUES (?,?,?,?,?,?,?)''',
                    ('Hedef (Yerel Docker)', '', '', '',
                     tgt.get('target_docker_container', 'postgres'),
                     tgt.get('target_pg_user', 'postgres'),
                     tgt.get('target_pg_password', ''))
                )
            elif tgt_type == 'local':
                c.execute(
                    '''INSERT OR IGNORE INTO servers
                       (name, ssh_host, ssh_user, ssh_key, docker_container, pg_user, pg_password)
                       VALUES (?,?,?,?,?,?,?)''',
                    ('Hedef (Yerel PG)', '', '', '', '',
                     tgt.get('target_pg_user', 'postgres'),
                     tgt.get('target_pg_password', ''))
                )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Global config (keep_backups only)
# ---------------------------------------------------------------------------

def get_config() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT key, value FROM config')
    rows = c.fetchall()
    conn.close()
    return {k: v for k, v in rows}


def save_config(data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for k, v in data.items():
        c.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (k, str(v)))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Servers CRUD
# ---------------------------------------------------------------------------

def get_servers() -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, name, ssh_host, ssh_user, ssh_key, docker_container, pg_user FROM servers ORDER BY name')
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def get_server(server_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM servers WHERE id=?', (server_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    cols = [d[0] for d in c.description]
    conn.close()
    return dict(zip(cols, row))


def add_server(data: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        '''INSERT INTO servers (name, ssh_host, ssh_user, ssh_key, ssh_password,
                                docker_container, pg_user, pg_password)
           VALUES (:name, :ssh_host, :ssh_user, :ssh_key, :ssh_password,
                   :docker_container, :pg_user, :pg_password)''',
        {
            'name': data['name'],
            'ssh_host': data.get('ssh_host', ''),
            'ssh_user': data.get('ssh_user', ''),
            'ssh_key': data.get('ssh_key', ''),
            'ssh_password': data.get('ssh_password', ''),
            'docker_container': data.get('docker_container', ''),
            'pg_user': data.get('pg_user', 'postgres'),
            'pg_password': data.get('pg_password', ''),
        }
    )
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id


def update_server(server_id: int, data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    updatable = ['name', 'ssh_host', 'ssh_user', 'ssh_key', 'docker_container', 'pg_user']
    sets, params = [], []
    for f in updatable:
        if f in data:
            sets.append(f'{f}=?')
            params.append(data[f])
    # Passwords: update only if non-empty string sent
    for pwd_field in ('ssh_password', 'pg_password'):
        if data.get(pwd_field):
            sets.append(f'{pwd_field}=?')
            params.append(data[pwd_field])
    if sets:
        params.append(server_id)
        c.execute(f"UPDATE servers SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    conn.close()


def delete_server(server_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM servers WHERE id=?', (server_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def log_backup(server_id, server_name, database, started_at, finished_at,
               status, message, file_path=None, file_size=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO backup_logs
            (server_id, server_name, database, started_at, finished_at, status, message, file_path, file_size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (server_id, server_name, database, started_at, finished_at, status, message, file_path, file_size))
    conn.commit()
    conn.close()


def get_logs(limit=100) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM backup_logs ORDER BY id DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def get_backup_file_info() -> dict:
    """Returns {filename: {server_id, server_name, database}} from backup_logs."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT file_path, server_id, server_name, database FROM backup_logs WHERE file_path IS NOT NULL')
    rows = c.fetchall()
    conn.close()
    result = {}
    for file_path, server_id, server_name, database in rows:
        if file_path:
            fname = os.path.basename(file_path)
            result[fname] = {
                'server_id': server_id,
                'server_name': server_name or '',
                'database': database or '',
            }
    return result
