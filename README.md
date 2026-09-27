Running the app
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export APP_PASSWORD='<en az 12 karakter>' SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
python app.py          # http://127.0.0.1:5000 (waitress)
APP_PASSWORD ve SECRET_KEY zorunludur; verilmezse uygulama başlamaz. Ağ ayarları: BIND_HOST, PORT, TRUSTED_PROXIES, SESSION_COOKIE_SECURE, ALLOWED_IPS (bkz. setup.sh).

Production deploy: bash setup.sh — installs deps, creates systemd service, generates random password.

Architecture
Three Python files with clear responsibilities:

config.py — all SQLite access. Owns three tables: config (key/value global settings), servers (connection profiles), backup_logs. Also holds APP_USERNAME / APP_PASSWORD_HASH module-level constants read from env at import time.
backup.py — all subprocess execution (SSH, Docker, pg_dump, psql, gzip). No Flask imports. Stateless functions that receive a server dict.
app.py — Flask routes + APScheduler setup. Thin layer: validates input, loads server dicts from config, calls backup, returns JSON. All routes (except /login) protected by @login_required session decorator.
Server model
A server dict drives all backup/restore logic. Four connection combinations are possible based on two boolean flags:

ssh_host set?	docker_container set?	Result
yes	yes	SSH → docker exec (original use case)
yes	no	SSH → local psql on remote host
no	yes	Local docker exec
no	no	Local psql
Helpers in backup.py: _is_remote(server), _is_docker(server), _ssh_prefix(server) (returns (cmd_list, env_dict_or_None) — env carries SSHPASS when ssh_password is set).

Key conventions
_ssh_prefix return value must always be unpacked and env threaded through subprocess calls:

ssh, ssh_env = _ssh_prefix(server)
env = _merge_env(ssh_env)   # merges into os.environ or returns None
subprocess.run(ssh + [cmd], env=env, ...)
Forgetting env= silently drops the SSHPASS variable and causes auth failure.

Passwords are never sent to the frontend. get_servers() intentionally omits ssh_password and pg_password columns (the SELECT lists columns explicitly). /api/config strips password keys before returning.

Scheduled jobs call backup.run_backup_server(server_id, database) — it re-fetches the server from DB each run so config changes take effect without restarting the scheduler.

Backup filenames: {safe_server_name}_{safe_db_name}_{YYYYMMDD}_{HHMMSS}.sql.gz where safe = alphanumeric + hyphens only. list_backups() joins against backup_logs to get server/db metadata rather than parsing filenames.

SQLite migrations
init_db() uses ALTER TABLE … ADD COLUMN wrapped in try/except for additive migrations. It also auto-migrates old single-server config (pre-multi-server schema) into the first servers row on startup.

Frontend
templates/index.html is a single-page app (Bootstrap 5 + vanilla JS, no build step). Global _servers array is populated once on load and reused to populate all dropdowns. No server-side templating beyond the login error message in templates/login.html.
