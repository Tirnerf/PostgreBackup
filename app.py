import os
import threading
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from werkzeug.security import check_password_hash

import config as cfg_module
import backup

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')
cfg_module.init_db()

jobstores = {'default': SQLAlchemyJobStore(url='sqlite:///data/scheduler.db')}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone='UTC')
scheduler.start()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Oturum açmanız gerekiyor.'}), 401
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


@app.get('/login')
def login_page():
    if session.get('logged_in'):
        return redirect(url_for('index'))
    return render_template('login.html', error=None)


@app.post('/login')
def do_login():
    data = request.form
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if username == cfg_module.APP_USERNAME and check_password_hash(cfg_module.APP_PASSWORD_HASH, password):
        session['logged_in'] = True
        return redirect(url_for('index'))
    return render_template('login.html', error='Kullanıcı adı veya şifre hatalı.')


@app.post('/logout')
def do_logout():
    session.clear()
    return redirect(url_for('login_page'))


# ---------------------------------------------------------------------------
# Servers API
# ---------------------------------------------------------------------------

@app.get('/api/servers')
@login_required
def api_get_servers():
    return jsonify(cfg_module.get_servers())


@app.post('/api/servers')
@login_required
def api_add_server():
    data = request.json or {}
    if not data.get('name', '').strip():
        return jsonify({'error': 'Sunucu adı gerekli.'}), 400
    try:
        new_id = cfg_module.add_server(data)
        return jsonify({'status': 'ok', 'id': new_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.put('/api/servers/<int:server_id>')
@login_required
def api_update_server(server_id):
    data = request.json or {}
    if not cfg_module.get_server(server_id):
        return jsonify({'error': 'Sunucu bulunamadı.'}), 404
    try:
        cfg_module.update_server(server_id, data)
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.delete('/api/servers/<int:server_id>')
@login_required
def api_delete_server(server_id):
    if not cfg_module.get_server(server_id):
        return jsonify({'error': 'Sunucu bulunamadı.'}), 404
    cfg_module.delete_server(server_id)
    return jsonify({'status': 'ok'})


@app.post('/api/servers/<int:server_id>/test')
@login_required
def api_test_server(server_id):
    server = cfg_module.get_server(server_id)
    if not server:
        return jsonify({'error': 'Sunucu bulunamadı.'}), 404
    return jsonify(backup.test_server_connection(server))


@app.get('/api/servers/<int:server_id>/databases')
@login_required
def api_server_databases(server_id):
    server = cfg_module.get_server(server_id)
    if not server:
        return jsonify({'error': 'Sunucu bulunamadı.'}), 404
    return jsonify(backup.list_server_databases(server))


# ---------------------------------------------------------------------------
# Global config
# ---------------------------------------------------------------------------

@app.get('/api/config')
@login_required
def api_get_config():
    cfg = cfg_module.get_config()
    return jsonify({'keep_backups': cfg.get('keep_backups', '7')})


@app.post('/api/config')
@login_required
def api_save_config():
    data = request.json or {}
    cfg_module.save_config({'keep_backups': str(data.get('keep_backups', '7'))})
    return jsonify({'status': 'ok'})


# ---------------------------------------------------------------------------
# On-demand backup
# ---------------------------------------------------------------------------

@app.post('/api/backup/now')
@login_required
def api_backup_now():
    data = request.json or {}
    server_id = data.get('server_id')
    database = data.get('database', '').strip()
    if not server_id or not database:
        return jsonify({'error': 'server_id ve database gerekli.'}), 400
    server = cfg_module.get_server(int(server_id))
    if not server:
        return jsonify({'error': 'Sunucu bulunamadı.'}), 404
    t = threading.Thread(target=backup.run_backup_now, args=[server, database], daemon=True)
    t.start()
    return jsonify({'status': 'started'})


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

@app.get('/api/jobs')
@login_required
def api_list_jobs():
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'name': job.name,
            'next_run': job.next_run_time.isoformat() if job.next_run_time else None,
            'trigger': str(job.trigger),
        })
    return jsonify(jobs)


@app.post('/api/jobs')
@login_required
def api_create_job():
    data = request.json or {}
    server_id = data.get('server_id')
    database = data.get('database', '').strip()
    if not server_id or not database:
        return jsonify({'error': 'server_id ve database gerekli.'}), 400
    server = cfg_module.get_server(int(server_id))
    if not server:
        return jsonify({'error': 'Sunucu bulunamadı.'}), 404

    freq_type = data.get('freq_type', 'interval')
    job_id = f'backup_{server_id}_{database}'

    try:
        if freq_type == 'interval':
            trigger = IntervalTrigger(hours=max(1, int(data.get('hours', 24))))
        elif freq_type == 'daily':
            trigger = CronTrigger(hour=int(data.get('hour', 2)), minute=int(data.get('minute', 0)))
        elif freq_type == 'weekly':
            trigger = CronTrigger(day_of_week=int(data.get('dow', 0)),
                                  hour=int(data.get('hour', 2)), minute=int(data.get('minute', 0)))
        else:
            trigger = CronTrigger.from_crontab(data.get('cron', '').strip())

        scheduler.add_job(
            backup.run_backup_server,
            trigger=trigger,
            id=job_id,
            name=f'Backup: {server["name"]} / {database}',
            args=[int(server_id), database],
            replace_existing=True,
            misfire_grace_time=3600,
        )
        return jsonify({'status': 'ok', 'job_id': job_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.delete('/api/jobs/<job_id>')
@login_required
def api_delete_job(job_id):
    job = scheduler.get_job(job_id)
    if not job:
        return jsonify({'error': 'İş bulunamadı.'}), 404
    scheduler.remove_job(job_id)
    return jsonify({'status': 'ok'})


# ---------------------------------------------------------------------------
# Backup file listing
# ---------------------------------------------------------------------------

@app.get('/api/backups')
@login_required
def api_list_backups():
    return jsonify(backup.list_backups())


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

@app.post('/api/restore')
@login_required
def api_restore():
    data = request.json or {}
    filename = data.get('filename', '').strip()
    target_server_id = data.get('target_server_id')
    database = data.get('database', '').strip()

    if not all([filename, target_server_id, database]):
        return jsonify({'error': 'Eksik parametre.'}), 400
    if '/' in filename or '\\' in filename or '..' in filename:
        return jsonify({'error': 'Geçersiz dosya adı.'}), 400

    target_server = cfg_module.get_server(int(target_server_id))
    if not target_server:
        return jsonify({'error': 'Hedef sunucu bulunamadı.'}), 404

    result = backup.restore_backup(filename, target_server, database)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@app.get('/api/logs')
@login_required
def api_get_logs():
    limit = int(request.args.get('limit', 100))
    return jsonify(cfg_module.get_logs(limit))


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

@app.get('/')
@login_required
def index():
    return render_template('index.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
