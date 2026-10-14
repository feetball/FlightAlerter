import base64
import hmac
import logging
import os
import threading
import time
from collections import deque

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from waitress import serve

import flightAlerter as alerter
import settings as config


logger = logging.getLogger('flightAlerter')

app = Flask(__name__, static_folder='static', static_url_path='/static')

LOG_LINES = deque(maxlen=300)
HISTORY = deque(maxlen=180)

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

live_cache = {'fetched_at': 0, 'url': None, 'payload': None}
live_cache_lock = threading.Lock()


class MemoryLogHandler(logging.Handler):
    def emit(self, record):
        LOG_LINES.append({'time': record.created, 'level': record.levelname, 'message': record.getMessage()})


def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logger.addHandler(MemoryLogHandler())
    logger.setLevel(logging.INFO)


def summarize_result(result):
    summary = dict(result)
    for key in ('military_seen', 'military_in_range', 'notified'):
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
            result = {'started_at': time.time(), 'ok': False, 'error': repr(exc),
                      'aircraft_total': 0, 'aircraft_positioned': 0,
                      'military_seen': [], 'military_in_range': [], 'notified': [], 'email_sent': False}
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


@app.before_request
def require_password():
    password = os.environ.get('WEB_PASSWORD')
    if not password:
        return None

    supplied = ''
    header = request.headers.get('Authorization', '')
    if header.startswith('Basic '):
        try:
            supplied = base64.b64decode(header[6:]).decode('utf-8').split(':', 1)[1]
        except (ValueError, IndexError):
            supplied = ''
    if hmac.compare_digest(supplied.encode('utf-8'), password.encode('utf-8')):
        return None
    return Response('Login required', 401, {'WWW-Authenticate': 'Basic realm="Flight Alerter"'})


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/status')
def api_status():
    settings = config.load_settings()
    smtp = alerter.smtp_config()
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
    with live_cache_lock:
        if live_cache['url'] == url and time.time() - live_cache['fetched_at'] < 2:
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
        alerter.annotate_aircraft(flight, home_coords)
        if flight['military']:
            alerter.enrich_flight_with_aircraft_db(flight, db_url)
        aircraft.append(alerter.summarize_aircraft(flight))

    payload = {
        'now': data.get('now'),
        'home': {'lat': settings['home_lat'], 'lon': settings['home_lon']},
        'radius_miles': settings['alert_radius_miles'],
        'aircraft': aircraft,
    }
    with live_cache_lock:
        live_cache.update({'fetched_at': time.time(), 'url': url, 'payload': payload})
    return jsonify(payload)


@app.route('/api/alerts')
def api_alerts():
    with alerter.SENT_ALERTS_LOCK:
        sent_alerts = alerter.load_sent_alerts()
    rows = [{'key': str(row.flight_num), 'flight': str(row.flight_name).strip(), 'sent_at': float(row.sent_time)}
            for row in sent_alerts.itertuples()]
    rows.sort(key=lambda row: row['sent_at'], reverse=True)
    return jsonify({'alerts': rows[:200], 'total': len(rows)})


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
        return jsonify(config.load_settings())

    changes = request.get_json(silent=True)
    if not isinstance(changes, dict):
        return jsonify({'errors': {'_': 'expected a JSON object'}}), 400
    settings, errors = config.update_settings(changes)
    if errors:
        return jsonify({'errors': errors, 'settings': settings}), 400

    logger.info('Settings updated: ' + ', '.join(sorted(changes)))
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
    threading.Thread(target=scheduler_loop, name='scheduler', daemon=True).start()
    port = int(os.environ.get('WEB_PORT', '8095'))
    logger.info('Web UI listening on port ' + str(port))
    serve(app, host='0.0.0.0', port=port, threads=8)


if __name__ == '__main__':
    main()
