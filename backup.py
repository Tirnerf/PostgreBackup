import subprocess
import os
from datetime import datetime

import config as cfg_module
from config import BACKUP_DIR, log_backup, get_backup_file_info


def _ssh_prefix(server: dict) -> tuple[list, dict | None]:
    """Returns (full_ssh_command_prefix, extra_env_or_None).

    If ssh_password is set, uses sshpass with SSHPASS env var (avoids exposing
    password in process list). Caller must merge extra_env into subprocess env.
    """
    ssh_password = server.get('ssh_password', '').strip()

    if ssh_password:
        prefix = ['sshpass', '-e']
        extra_env = {'SSHPASS': ssh_password}
    else:
        prefix = []
        extra_env = None

    args = prefix + ['ssh']
    if server.get('ssh_key', '').strip():
        args += ['-i', server['ssh_key'].strip()]
    args += ['-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10']
    if not ssh_password:
        # BatchMode=yes devre dışı bırakır interaktif şifre istemini;
        # sshpass kullanılıyorsa gerekmez
        args += ['-o', 'BatchMode=yes']
    args += [f"{server['ssh_user']}@{server['ssh_host']}"]
    return args, extra_env


def _merge_env(extra: dict | None) -> dict | None:
    """Merges extra_env into os.environ for subprocess calls."""
    if not extra:
        return None
    return {**os.environ, **extra}


def _is_remote(server: dict) -> bool:
    return bool(server.get('ssh_host', '').strip())


def _is_docker(server: dict) -> bool:
    return bool(server.get('docker_container', '').strip())


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------

def test_server_connection(server: dict) -> dict:
    try:
        if _is_remote(server):
            ssh, ssh_env = _ssh_prefix(server)
            env = _merge_env(ssh_env)
            r = subprocess.run(ssh + ['echo ok'], capture_output=True, text=True, timeout=15, env=env)
            if r.returncode != 0:
                return {'status': 'error', 'message': f'SSH hatası: {r.stderr.strip()}'}
            if _is_docker(server):
                container = server['docker_container']
                r2 = subprocess.run(
                    ssh + [f'docker inspect --format "{{{{.State.Status}}}}" {container}'],
                    capture_output=True, text=True, timeout=15, env=env
                )
                state = r2.stdout.strip().strip('"')
                if state == 'running':
                    return {'status': 'ok', 'message': 'SSH bağlantısı başarılı, container çalışıyor.'}
                return {'status': 'warning', 'message': f'SSH OK ama container durumu: {state or r2.stderr.strip()}'}
            return {'status': 'ok', 'message': 'SSH bağlantısı başarılı.'}
        else:
            if _is_docker(server):
                container = server['docker_container']
                r = subprocess.run(
                    ['docker', 'inspect', '--format', '{{.State.Status}}', container],
                    capture_output=True, text=True, timeout=15
                )
                state = r.stdout.strip()
                if state == 'running':
                    return {'status': 'ok', 'message': f'Container "{container}" çalışıyor.'}
                return {'status': 'warning', 'message': f'Container durumu: {state or r.stderr.strip()}'}
            env = os.environ.copy()
            if server.get('pg_password'):
                env['PGPASSWORD'] = server['pg_password']
            r = subprocess.run(
                ['psql', '-U', server.get('pg_user', 'postgres'), '-d', 'postgres', '-c', 'SELECT 1'],
                capture_output=True, text=True, timeout=15, env=env
            )
            if r.returncode == 0:
                return {'status': 'ok', 'message': 'Yerel PostgreSQL bağlantısı başarılı.'}
            return {'status': 'error', 'message': r.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {'status': 'error', 'message': 'Bağlantı zaman aşımı.'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ---------------------------------------------------------------------------
# List databases
# ---------------------------------------------------------------------------

def list_server_databases(server: dict) -> dict:
    query = "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname;"
    pg_user = server.get('pg_user', 'postgres')
    pg_password = server.get('pg_password', '')
    try:
        if _is_remote(server):
            ssh, ssh_env = _ssh_prefix(server)
            env = _merge_env(ssh_env)
            if _is_docker(server):
                remote_cmd = f'docker exec {server["docker_container"]} psql -U {pg_user} -d postgres -t -A -c "{query}"'
            else:
                pw = f"PGPASSWORD='{pg_password}' " if pg_password else ''
                remote_cmd = f'{pw}psql -U {pg_user} -d postgres -t -A -c "{query}"'
            r = subprocess.run(ssh + [remote_cmd], capture_output=True, text=True, timeout=20, env=env)
        else:
            env = os.environ.copy()
            if pg_password:
                env['PGPASSWORD'] = pg_password
            if _is_docker(server):
                r = subprocess.run(
                    ['docker', 'exec', server['docker_container'],
                     'psql', '-U', pg_user, '-d', 'postgres', '-t', '-A', '-c', query],
                    capture_output=True, text=True, timeout=20
                )
            else:
                r = subprocess.run(
                    ['psql', '-U', pg_user, '-d', 'postgres', '-t', '-A', '-c', query],
                    capture_output=True, text=True, timeout=20, env=env
                )
        if r.returncode != 0:
            return {'status': 'error', 'message': r.stderr.strip()}
        dbs = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        return {'status': 'ok', 'databases': dbs}
    except subprocess.TimeoutExpired:
        return {'status': 'error', 'message': 'Sorgu zaman aşımı.'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def run_backup_server(server_id: int, database: str):
    """APScheduler entry point."""
    server = cfg_module.get_server(server_id)
    if server:
        _do_backup(server, database)


def run_backup_now(server: dict, database: str):
    """On-demand backup entry point."""
    _do_backup(server, database)


def _safe_name(s: str) -> str:
    return ''.join(c if c.isalnum() or c in '-' else '_' for c in s)


def _do_backup(server: dict, database: str):
    server_id = server['id']
    server_name = server['name']
    started_at = datetime.now().isoformat()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(BACKUP_DIR, exist_ok=True)

    filename = f"{_safe_name(server_name)}_{_safe_name(database)}_{timestamp}.sql.gz"
    filepath = os.path.join(BACKUP_DIR, filename)

    pg_user = server.get('pg_user', 'postgres')
    pg_password = server.get('pg_password', '')

    try:
        if _is_remote(server):
            ssh, ssh_env = _ssh_prefix(server)
            s_env = _merge_env(ssh_env)
            if _is_docker(server):
                container = server['docker_container']
                if pg_password:
                    remote_cmd = (
                        f"PGPASSWORD='{pg_password}' docker exec "
                        f"-e PGPASSWORD='{pg_password}' {container} "
                        f"pg_dump -U {pg_user} --no-password {database}"
                    )
                else:
                    remote_cmd = f"docker exec {container} pg_dump -U {pg_user} {database}"
            else:
                pw = f"PGPASSWORD='{pg_password}' " if pg_password else ''
                remote_cmd = f"{pw}pg_dump -U {pg_user} {database}"
            p_dump = subprocess.Popen(ssh + [remote_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env=s_env)
        else:
            env = os.environ.copy()
            if pg_password:
                env['PGPASSWORD'] = pg_password
            if _is_docker(server):
                container = server['docker_container']
                p_dump = subprocess.Popen(
                    ['docker', 'exec', container, 'pg_dump', '-U', pg_user, database],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            else:
                p_dump = subprocess.Popen(
                    ['pg_dump', '-U', pg_user, database],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
                )

        with open(filepath, 'wb') as out_file:
            p_gz = subprocess.Popen(['gzip'], stdin=p_dump.stdout, stdout=out_file, stderr=subprocess.PIPE)
            p_dump.stdout.close()
            p_gz.wait()
            p_dump.wait()

        cfg = cfg_module.get_config()
        keep = int(cfg.get('keep_backups', '7'))

        if p_dump.returncode == 0 and p_gz.returncode == 0:
            file_size = os.path.getsize(filepath)
            finished_at = datetime.now().isoformat()
            log_backup(server_id, server_name, database, started_at, finished_at,
                       'success', f'Yedek alındı: {filename}', filepath, file_size)
            _cleanup_old_backups(_safe_name(server_name), _safe_name(database), keep)
        else:
            dump_err = p_dump.stderr.read().decode(errors='replace')
            gz_err = p_gz.stderr.read().decode(errors='replace')
            error_msg = (dump_err + gz_err).strip() or 'Bilinmeyen hata'
            if os.path.exists(filepath):
                os.remove(filepath)
            finished_at = datetime.now().isoformat()
            log_backup(server_id, server_name, database, started_at, finished_at, 'error', error_msg)

    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        finished_at = datetime.now().isoformat()
        log_backup(server_id, server_name, database, started_at, finished_at, 'error', str(e))


def _cleanup_old_backups(safe_server: str, safe_db: str, keep: int):
    prefix = f"{safe_server}_{safe_db}_"
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith(prefix) and f.endswith('.sql.gz'))
    for old in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# List backup files
# ---------------------------------------------------------------------------

def list_backups() -> list:
    if not os.path.exists(BACKUP_DIR):
        return []

    file_info = get_backup_file_info()

    files = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not f.endswith('.sql.gz'):
            continue
        path = os.path.join(BACKUP_DIR, f)
        info = file_info.get(f)
        if info:
            db_name = info['database']
            server_name = info['server_name']
            server_id = info['server_id']
        else:
            # Fallback: old filename format db_YYYYMMDD_HHMMSS.sql.gz
            stem = f[:-len('.sql.gz')]
            parts = stem.rsplit('_', 2)
            db_name = parts[0] if len(parts) == 3 else stem
            server_name = ''
            server_id = None

        files.append({
            'filename': f,
            'server_id': server_id,
            'server_name': server_name,
            'database': db_name,
            'size': os.path.getsize(path),
            'modified': datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
        })
    return files


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_backup(filename: str, target_server: dict, database: str) -> dict:
    filepath = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(filepath):
        return {'status': 'error', 'message': 'Dosya bulunamadı.'}

    pg_user = target_server.get('pg_user', 'postgres')
    pg_password = target_server.get('pg_password', '')

    try:
        _create_db(target_server, database)

        if _is_remote(target_server):
            ssh, ssh_env = _ssh_prefix(target_server)
            s_env = _merge_env(ssh_env)
            if _is_docker(target_server):
                container = target_server['docker_container']
                pw_env = f"-e PGPASSWORD='{pg_password}'" if pg_password else ''
                restore_cmd = f"docker exec -i {pw_env} {container} psql -U {pg_user} -d {database}"
            else:
                pw = f"PGPASSWORD='{pg_password}' " if pg_password else ''
                restore_cmd = f"{pw}psql -U {pg_user} -d {database}"

            p_gz = subprocess.Popen(['gunzip', '-c', filepath], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p_psql = subprocess.Popen(ssh + [restore_cmd], stdin=p_gz.stdout,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=s_env)
        else:
            env = os.environ.copy()
            if pg_password:
                env['PGPASSWORD'] = pg_password
            if _is_docker(target_server):
                container = target_server['docker_container']
                p_gz = subprocess.Popen(['gunzip', '-c', filepath], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                p_psql = subprocess.Popen(
                    ['docker', 'exec', '-i', container, 'psql', '-U', pg_user, '-d', database],
                    stdin=p_gz.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            else:
                p_gz = subprocess.Popen(['gunzip', '-c', filepath], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                p_psql = subprocess.Popen(
                    ['psql', '-U', pg_user, '-d', database],
                    stdin=p_gz.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
                )

        p_gz.stdout.close()
        _, psql_err = p_psql.communicate(timeout=600)
        p_gz.wait()

        if p_psql.returncode == 0:
            return {'status': 'ok', 'message': f'"{database}" → "{target_server["name"]}" başarıyla geri yüklendi.'}
        return {'status': 'error', 'message': psql_err.decode(errors='replace').strip() or 'Restore hatası'}

    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def _create_db(server: dict, database: str):
    pg_user = server.get('pg_user', 'postgres')
    pg_password = server.get('pg_password', '')
    create_sql = f'CREATE DATABASE "{database}"'

    try:
        if _is_remote(server):
            ssh, ssh_env = _ssh_prefix(server)
            s_env = _merge_env(ssh_env)
            if _is_docker(server):
                container = server['docker_container']
                pw_env = f"-e PGPASSWORD='{pg_password}'" if pg_password else ''
                cmd = f'docker exec {pw_env} {container} psql -U {pg_user} -d postgres -c "{create_sql}" 2>/dev/null || true'
            else:
                pw = f"PGPASSWORD='{pg_password}' " if pg_password else ''
                cmd = f'{pw}psql -U {pg_user} -d postgres -c "{create_sql}" 2>/dev/null || true'
            subprocess.run(ssh + [cmd], capture_output=True, timeout=30, env=s_env)
        else:
            env = os.environ.copy()
            if pg_password:
                env['PGPASSWORD'] = pg_password
            if _is_docker(server):
                container = server['docker_container']
                subprocess.run(
                    ['docker', 'exec', container, 'psql', '-U', pg_user, '-d', 'postgres', '-c', create_sql],
                    capture_output=True, timeout=30
                )
            else:
                subprocess.run(
                    ['psql', '-U', pg_user, '-d', 'postgres', '-c', create_sql],
                    capture_output=True, timeout=30, env=env
                )
    except Exception:
        pass
