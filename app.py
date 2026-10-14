import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque
from datetime import timedelta

import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from waitress import serve

import flightAlerter as alerter
import secret_store
import settings as config


logger = logging.getLogger('flightAlerter')

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.config.update(
    SESSION_COOKIE_NAME='flightalerter_session',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
    # Set WEB_HTTPS=1 when the dashboard is served over HTTPS (e.g. behind a reverse proxy).
    SESSION_COOKIE_SECURE=os.environ.get('WEB_HTTPS') == '1',
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

LOG_LINES = deque(maxlen=300)
HISTORY = deque(maxlen=180)

# Endpoints reachable without logging in.
PUBLIC_ENDPOINTS = {'login_page', 'static', 'api_auth_status', 'api_auth_login', 'api_auth_setup'}
CSRF_HEADER = 'X-Requested-With'
CSRF_VALUE = 'FlightAlerter'
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60
MIN_PASSWORD_LENGTH = 8

state = {
    'started_at': time.time(),
    'running': False,
    'runs': 0,
    'last_result': None,
    'last_email_at': None,
    'next_run_at': None,
}
state_lock = threading.Lock()
run_lock = threading.Lock()
wake_scheduler = threading.Event()

live_cache = {'fetched_at': 0, 'key': None, 'payload': None}
live_cache_lock = threading.Lock()

login_failures = {}
login_failures_lock = threading.Lock()
setup_code = {'value': None}


class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        LOG_LINES.append({'time': record.created, 'level': record.levelname, 'message': record.getMessage()})


def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logger.addHandler(MemoryLogHandler())
    logger.setLevel(logging.INFO)


# ---------- authentication ----------

def prepare_auth():
    app.secret_key = secret_store.session_key()
    if secret_store.admin_configured():
        return
    seed_password = os.environ.get('WEB_PASSWORD', '')
    if seed_password:
        secret_store.set_admin_password(seed_password)
        logger.info('Admin password set from WEB_PASSWORD in .env')
        return
    setup_code['value'] = secrets.token_hex(4).upper()
    # Deliberately bypasses the in-app log so only someone with access to the server's logs sees it.
    print('=' * 60, flush=True)
    print('FIRST-TIME SETUP: open the dashboard and enter this setup code: ' + setup_code['value'], flush=True)
    print('=' * 60, flush=True)
    logger.warning('No admin password yet. The setup code is printed in the server log (docker logs).')


def is_logged_in():
    return (session.get('user') == 'admin'
            and session.get('password_version') == secret_store.get('password_version'))


def start_session(password_version):
    session.clear()
    session.permanent = True
    session['user'] = 'admin'
    session['password_version'] = password_version


def client_address():
    return request.remote_addr or 'unknown'


def login_locked_for():
    """Seconds until this client may try again, or 0."""
    now = time.time()
    with login_failures_lock:
        attempts = [t for t in login_failures.get(client_address(), []) if now - t < LOGIN_LOCKOUT_SECONDS]
        login_failures[client_address()] = attempts
        if len(attempts) >= MAX_LOGIN_FAILURES:
            return int(LOGIN_LOCKOUT_SECONDS - (now - attempts[0])) + 1
    return 0


def record_login_failure():
    with login_failures_lock:
        login_failures.setdefault(client_address(), []).append(time.time())
    logger.warning('Failed login attempt from ' + client_address())


def clear_login_failures():
    with login_failures_lock:
        login_failures.pop(client_address(), None)


def too_many_attempts(seconds):
    minutes = max(1, round(seconds / 60))
    return jsonify({'error': 'Too many failed attempts. Try again in ' + str(minutes) + ' minute(s).'}), 429


def password_problem(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return 'Password must be at least ' + str(MIN_PASSWORD_LENGTH) + ' characters.'
    if len(password) > 256:
        return 'Password must be at most 256 characters.'
    return None


@app.before_request
def guard_request():
    # Cross-site pages can't add custom headers without a CORS preflight, which this app never allows,
    # so requiring one on every state-changing request blocks CSRF.
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        return jsonify({'error': 'Missing request header'}), 403

    if request.endpoint in PUBLIC_ENDPOINTS or is_logged_in():
        return None
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Login required'}), 401
    return redirect('/login')


@app.after_request
def security_headers(response):
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "img-src 'self' data: https://tile.openstreetmap.org https://cdnjs.cloudflare.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/login')
def login_page():
    if is_logged_in():
        return redirect('/')
    return send_from_directory(app.static_folder, 'login.html')


@app.route('/api/auth/status')
def api_auth_status():
    return jsonify({'configured': secret_store.admin_configured(), 'logged_in': is_logged_in()})


@app.route('/api/auth/setup', methods=['POST'])
def api_auth_setup():
    if secret_store.admin_configured():
        return jsonify({'error': 'An admin password is already set.'}), 409
    locked = login_locked_for()
    if locked:
        return too_many_attempts(locked)

    body = request.get_json(silent=True) or {}
    code = str(body.get('code', '')).strip().upper()
    if not setup_code['value'] or not hmac.compare_digest(code.encode(), setup_code['value'].encode()):
        record_login_failure()
        return jsonify({'error': 'That setup code is not right. Check the server log.'}), 403
    problem = password_problem(body.get('password'))
    if problem:
        return jsonify({'error': problem}), 400

    version = secret_store.set_admin_password(body['password'])
    setup_code['value'] = None
    clear_login_failures()
    start_session(version)
    logger.info('Admin password created')
    return jsonify({'ok': True})


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    locked = login_locked_for()
    if locked:
        return too_many_attempts(locked)

    body = request.get_json(silent=True) or {}
    if not secret_store.verify_admin_password(str(body.get('password', ''))):
        record_login_failure()
        return jsonify({'error': 'Wrong password.'}), 403

    clear_login_failures()
    start_session(secret_store.get('password_version'))
    logger.info('Signed in from ' + client_address())
    return jsonify({'ok': True})


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/auth/password', methods=['POST'])
def api_auth_password():
    locked = login_locked_for()
    if locked:
        return too_many_attempts(locked)

    body = request.get_json(silent=True) or {}
    if not secret_store.verify_admin_password(str(body.get('current', ''))):
        record_login_failure()
        return jsonify({'error': 'Current password is wrong.'}), 403
    problem = password_problem(body.get('new'))
    if problem:
        return jsonify({'error': problem}), 400

    # Bumping the version signs out every other session; keep this one signed in.
    start_session(secret_store.set_admin_password(body['new']))
    logger.info('Admin password changed; other sessions signed out')
    return jsonify({'ok': True})


# ---------- checks ----------

def summarize_result(result):
    summary = dict(result)
    for key in ('military_seen', 'military_in_range', 'watch_in_range', 'notified'):
        summary[key] = [alerter.summarize_aircraft(flight) for flight in result.get(key, [])]
    return summary


def run_once():
    with run_lock:
        with state_lock:
            state['running'] = True
        try:
            result = alerter.run_check(config.load_settings())
        except Exception as exc:
            logger.exception('Check failed')
            result = {'started_at': time.time(), 'ok': False, 'error': str(exc) or repr(exc),
                      'aircraft_total': 0, 'aircraft_positioned': 0, 'military_seen': [],
                      'military_in_range': [], 'watch_in_range': [], 'notified': [],
                      'emails_sent': 0, 'email_sent': False}
        result['finished_at'] = time.time()
        summary = summarize_result(result)

        with state_lock:
            state['running'] = False
            state['runs'] += 1
            state['last_result'] = summary
            if result['email_sent']:
                state['last_email_at'] = result['finished_at']
            HISTORY.append({
                'time': result['finished_at'],
                'ok': result['ok'],
                'positioned': result['aircraft_positioned'],
                'military': len(result['military_seen']),
                'in_range': len(result['military_in_range']),
            })
        return summary


def scheduler_loop():
    while True:
        settings = config.load_settings()
        if settings['enabled']:
            run_once()
        interval = settings['check_interval_seconds']
        with state_lock:
            state['next_run_at'] = time.time() + interval if settings['enabled'] else None
        # A settings change or "pause/resume" wakes the loop early.
        wake_scheduler.wait(interval)
        wake_scheduler.clear()


# ---------- pages & API ----------

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/status')
def api_status():
    settings = config.load_settings()
    smtp = alerter.smtp_config(settings)
    with state_lock:
        return jsonify({
            'now': time.time(),
            'started_at': state['started_at'],
            'running': state['running'],
            'runs': state['runs'],
            'next_run_at': state['next_run_at'],
            'last_email_at': state['last_email_at'],
            'last_result': state['last_result'],
            'history': list(HISTORY),
            'enabled': settings['enabled'],
            'send_emails': settings['send_emails'],
            'smtp': {'configured': smtp['configured'], 'hostname': smtp['hostname'], 'username': smtp['username']},
        })


@app.route('/api/aircraft')
def api_aircraft():
    """Live positions for the map. Cached briefly so several open tabs don't hammer the receiver."""
    settings = config.load_settings()
    url = settings['aircraft_data_url']
    cache_key = repr((url, settings['home_lat'], settings['home_lon'], settings['watchlist'], settings['alert_radius_miles']))
    with live_cache_lock:
        if live_cache['key'] == cache_key and time.time() - live_cache['fetched_at'] < 2:
            return jsonify(live_cache['payload'])

    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        return jsonify({'error': 'Could not reach the receiver: ' + str(exc)}), 502

    home_coords = (settings['home_lat'], settings['home_lon'])
    db_url = alerter.get_aircraft_db_url(url)
    aircraft = []
    for flight in data.get('aircraft', []):
        if 'lat' not in flight or 'lon' not in flight or flight.get('seen_pos', flight.get('seen', 999)) > 60:
            continue
        alerter.annotate_aircraft(flight, home_coords, db_url, settings['watchlist'])
        aircraft.append(alerter.summarize_aircraft(flight))

    payload = {
        'now': data.get('now'),
        'home': {'lat': settings['home_lat'], 'lon': settings['home_lon']},
        'radius_miles': settings['alert_radius_miles'],
        'watchlist': [{'id': entry['id'], 'label': alerter.watch_label(entry), 'match': entry['match'],
                       'radius_miles': entry['radius_miles'], 'enabled': entry['enabled']}
                      for entry in settings['watchlist']],
        'aircraft': aircraft,
    }
    with live_cache_lock:
        live_cache.update({'fetched_at': time.time(), 'key': cache_key, 'payload': payload})
    return jsonify(payload)


@app.route('/api/alerts')
def api_alerts():
    watch_labels = {entry['id']: alerter.watch_label(entry) for entry in config.load_settings()['watchlist']}
    with alerter.SENT_ALERTS_LOCK:
        sent_alerts = alerter.load_sent_alerts()
    rows = []
    for row in sent_alerts.itertuples():
        key = str(row.flight_num)
        kind, aircraft, watch = 'military', key, None
        if key.startswith('watch:') and key.count(':') == 2:
            _, entry_id, aircraft = key.split(':')
            kind, watch = 'watch', watch_labels.get(entry_id, 'removed watchlist entry')
        rows.append({'key': key, 'kind': kind, 'aircraft': aircraft, 'watch': watch,
                     'flight': str(row.flight_name).strip(), 'sent_at': float(row.sent_time)})
    rows.sort(key=lambda row: row['sent_at'], reverse=True)
    alerts = {}
    for row in rows:
        if row['key'] in alerts:
            alerts[row['key']]['count'] += 1
        else:
            alerts[row['key']] = {**row, 'count': 1}
    return jsonify({'alerts': list(alerts.values()), 'total': len(rows)})


@app.route('/api/alerts/<key>', methods=['DELETE'])
def api_reset_alerts(key):
    with alerter.SENT_ALERTS_LOCK:
        sent_alerts = alerter.load_sent_alerts()
        remaining = sent_alerts.loc[sent_alerts['flight_num'].astype(str) != key.lower()]
        removed = len(sent_alerts) - len(remaining)
        alerter.save_sent_alerts(remaining)
    logger.info('Reset alert history for ' + key + ' (' + str(removed) + ' removed)')
    return jsonify({'removed': removed})


@app.route('/api/settings', methods=['GET', 'PUT'])
def api_settings():
    if request.method == 'GET':
        return jsonify(config.public_settings())

    changes = request.get_json(silent=True)
    if not isinstance(changes, dict):
        return jsonify({'errors': {'_': 'expected a JSON object'}}), 400
    settings, errors = config.update_settings(changes)
    if errors:
        return jsonify({'errors': errors, 'settings': settings}), 400

    changed = sorted(key for key in changes if not (key == 'smtp_password' and not changes[key]))
    logger.info('Settings updated: ' + ', '.join(changed))
    wake_scheduler.set()
    return jsonify(settings)


@app.route('/api/run', methods=['POST'])
def api_run():
    return jsonify(run_once())


@app.route('/api/test-email', methods=['POST'])
def api_test_email():
    try:
        alerter.send_test_email(config.load_settings())
    except Exception as exc:
        logger.error('Test email failed: ' + repr(exc))
        return jsonify({'error': str(exc) or repr(exc)}), 502
    return jsonify({'ok': True})


@app.route('/api/receiver')
def api_receiver():
    """Receiver metadata (its own lat/lon); used by "use receiver location" and "test connection"."""
    url = request.args.get('url') or config.load_settings()['aircraft_data_url']
    if not url.startswith(('http://', 'https://')) or not url.split('?', 1)[0].endswith('/aircraft.json'):
        return jsonify({'ok': False, 'error': 'URL must point at .../aircraft.json'}), 400
    receiver_url = url.rsplit('/', 1)[0] + '/receiver.json'
    try:
        aircraft_response = requests.get(url, timeout=5)
        aircraft_response.raise_for_status()
        aircraft_count = len(aircraft_response.json().get('aircraft', []))
    except (requests.exceptions.RequestException, ValueError) as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 502

    receiver = {}
    try:
        receiver = requests.get(receiver_url, timeout=5).json()
    except (requests.exceptions.RequestException, ValueError):
        pass
    return jsonify({'ok': True, 'aircraft': aircraft_count, 'lat': receiver.get('lat'), 'lon': receiver.get('lon'),
                    'version': receiver.get('version')})


@app.route('/api/logs')
def api_logs():
    since = request.args.get('since', type=float, default=0)
    return jsonify({'lines': [line for line in LOG_LINES if line['time'] > since]})


def main():
    setup_logging()
    config.load_settings()  # migrates .env / CSV values into settings.json and secrets.json
    prepare_auth()
    threading.Thread(target=scheduler_loop, name='scheduler', daemon=True).start()
    port = int(os.environ.get('WEB_PORT', '8095'))
    logger.info('Web UI listening on port ' + str(port))
    serve(app, host='0.0.0.0', port=port, threads=8)


if __name__ == '__main__':
    main()
