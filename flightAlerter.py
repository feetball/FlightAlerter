import geopy.distance
import logging
import pandas as pd
import requests
import smtplib
import ssl
import threading
import time
import os
import re

from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime
from os.path import exists

import secret_store
import settings as config


logger = logging.getLogger('flightAlerter')

AIRCRAFT_DB_CACHE = {}
# The receiver's database is split into shard files; cache them so per-aircraft lookups stay cheap.
DB_SHARD_CACHE = {}
DB_SHARD_TTL_SECONDS = 24 * 3600
# After a failed shard fetch, don't retry it for a while so an unreachable database can't stall every cycle.
DB_SHARD_RETRY_SECONDS = 5 * 60
DB_SHARD_FAILURES = {}
SENT_ALERTS_FILENAME = os.path.join(config.DATA_DIR, 'sentAlerts.csv')
SENT_ALERT_COLUMNS = ['flight_num', 'flight_name', 'sent_time']
# Guards sentAlerts.csv, which both the check loop and the web UI touch.
SENT_ALERTS_LOCK = threading.Lock()


def is_military_aircraft(flight):
    db_flags = flight.get('dbFlags')
    if isinstance(db_flags, int) and (db_flags & 1) == 1:
        return True

    if flight.get('military') is True:
        return True

    operator_fields = [
        str(flight.get('ownOp', '')).upper(),
        str(flight.get('op', '')).upper(),
    ]
    operator_keywords = [
        'AIR FORCE', 'USAF', 'NAVY', 'MARINE', 'ARMY', 'COAST GUARD',
        'ROYAL AIR FORCE', 'RAF', 'LUFTWAFFE'
    ]
    if any(keyword in operator for operator in operator_fields for keyword in operator_keywords):
        return True

    callsign = str(flight.get('flight', '')).strip().upper()
    if re.match(r'^(RCH|MC|PAT|AE|VV|NAVY|REACH|KING|SPAR|GOLD)\d+', callsign):
        return True

    return False


def get_aircraft_db_url(aircraft_data_url):
    if '/data/' in aircraft_data_url:
        return aircraft_data_url.split('/data/', 1)[0] + '/db'

    return aircraft_data_url.rsplit('/', 1)[0] + '/db'


def fetch_db_shard(db_url, block_key, timeout_seconds):
    """Returns the shard dict, {} if the shard doesn't exist, or None on a transient error."""
    cache_key = db_url + '/' + block_key
    cached = DB_SHARD_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < DB_SHARD_TTL_SECONDS:
        return cached[1]
    failed_at = DB_SHARD_FAILURES.get(cache_key)
    if failed_at and time.time() - failed_at < DB_SHARD_RETRY_SECONDS:
        return None

    try:
        response = requests.get(db_url + '/' + block_key + '.json', timeout=timeout_seconds)
        if response.status_code == 404:
            shard_data = {}
        else:
            response.raise_for_status()
            shard_data = response.json()
    except (requests.exceptions.RequestException, ValueError):
        DB_SHARD_FAILURES[cache_key] = time.time()
        return None

    DB_SHARD_FAILURES.pop(cache_key, None)
    DB_SHARD_CACHE[cache_key] = (time.time(), shard_data)
    return shard_data


def fetch_aircraft_db_record(db_url, aircraft_hex, timeout_seconds=5):
    if not aircraft_hex:
        return {}

    icao = str(aircraft_hex).strip().upper()
    if not icao:
        return {}

    if icao in AIRCRAFT_DB_CACHE:
        return AIRCRAFT_DB_CACHE[icao]

    for level in range(1, len(icao) + 1):
        block_key = icao[:level]
        data_key = icao[level:]

        shard_data = fetch_db_shard(db_url, block_key, timeout_seconds)
        if shard_data is None:
            return {}  # transient failure: don't cache, try again next time

        if data_key in shard_data:
            AIRCRAFT_DB_CACHE[icao] = shard_data[data_key]
            return shard_data[data_key]

        children = shard_data.get('children', [])
        if not data_key or (block_key + data_key[:1]) not in children:
            AIRCRAFT_DB_CACHE[icao] = {}
            return {}

    AIRCRAFT_DB_CACHE[icao] = {}
    return {}


def enrich_flight_with_aircraft_db(flight, db_url):
    aircraft_hex = flight.get('hex')
    aircraft_db_record = fetch_aircraft_db_record(db_url, aircraft_hex)
    if not aircraft_db_record:
        return flight

    for key, value in aircraft_db_record.items():
        if flight.get(key) in (None, ''):
            flight[key] = value

    return flight


def get_aircraft_type(flight):
    direct_fields = [
        't', 'desc', 'aircraft_type', 'ac_type', 'model', 'man', 'icaoType'
    ]
    for field in direct_fields:
        value = flight.get(field)
        if value not in (None, ''):
            return str(value)

    if flight.get('dbFlags') not in (None, ''):
        return 'Unknown (dbFlags=' + str(flight.get('dbFlags')) + ')'

    if flight.get('category') not in (None, ''):
        return 'Unknown (category ' + str(flight.get('category')) + ')'

    return 'Unknown'

def fetch_aircraft_data(url, timeout_seconds=10, max_attempts=3, retry_delay_seconds=5):
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, timeout=timeout_seconds)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            logger.warning('Aircraft data fetch failed (attempt ' + str(attempt) + '/' + str(max_attempts) + '): ' + str(exc))
            if attempt < max_attempts:
                time.sleep(retry_delay_seconds)
        except ValueError as exc:
            logger.error('Aircraft data response was not valid JSON: ' + str(exc))
            return None

    logger.error('Unable to fetch aircraft data after retries. Skipping this cycle.')
    return None


def smtp_config(settings):
    password = secret_store.get('smtp_password') or ''
    return {
        'hostname': settings['smtp_hostname'],
        'port': settings['smtp_port'],
        'security': settings['smtp_security'],
        'username': settings['smtp_username'],
        'password': password,
        'from_name': settings['smtp_from_name'],
        'configured': bool(settings['smtp_hostname'] and settings['smtp_username'] and password),
    }


def normalize_ident(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def watch_entries_for(flight, watchlist):
    """Watchlist entries matching this aircraft by callsign, hex code or registration (tail number)."""
    idents = {normalize_ident(flight.get(field)) for field in ('flight', 'hex', 'r')} - {''}
    return [entry for entry in watchlist if entry['enabled'] and normalize_ident(entry['match']) in idents]


def annotate_aircraft(flight, home_coords, db_url, watchlist, db_lookup_radius=None):
    """Adds distance, database fields, military flag and matching watchlist entries.

    With db_lookup_radius, aircraft farther away are classified from the receiver's own fields only,
    skipping the database lookup (several HTTP requests for an aircraft not seen before)."""
    flight['distance'] = round(geopy.distance.distance(home_coords, (flight['lat'], flight['lon'])).mi, 1)
    if db_lookup_radius is None or flight['distance'] <= db_lookup_radius:
        enrich_flight_with_aircraft_db(flight, db_url)
    flight['military'] = is_military_aircraft(flight)
    flight['watch_entries'] = watch_entries_for(flight, watchlist)
    return flight


def summarize_aircraft(flight):
    return {
        'hex': flight.get('hex'),
        'flight': str(flight.get('flight') or '').strip(),
        'registration': flight.get('r'),
        'type': get_aircraft_type(flight),
        'lat': flight.get('lat'),
        'lon': flight.get('lon'),
        'altitude': flight.get('alt_baro'),
        'speed': flight.get('gs'),
        'track': flight.get('track'),
        'squawk': flight.get('squawk'),
        'distance': flight.get('distance'),
        'military': bool(flight.get('military')),
        'watched': [{'id': entry['id'], 'label': watch_label(entry), 'radius_miles': entry['radius_miles']}
                    for entry in flight.get('watch_entries', [])],
        'seen': flight.get('seen'),
    }


def watch_label(entry):
    return entry.get('label') or entry['match']


def load_sent_alerts():
    if exists(SENT_ALERTS_FILENAME):
        return pd.read_csv(SENT_ALERTS_FILENAME, dtype={'flight_num': str, 'flight_name': str})
    return pd.DataFrame(columns=SENT_ALERT_COLUMNS)


def save_sent_alerts(sent_alerts):
    sent_alerts.to_csv(SENT_ALERTS_FILENAME, index=False)


def alert_key(flight, prefix=''):
    return prefix + (str(flight.get('hex') or flight.get('flight') or 'unknown').strip().lower() or 'unknown')


def select_eligible_flights(flights, sent_alerts, settings, current_timestamp, key_prefix=''):
    eligible_flights = []
    for flight_info in flights:
        aircraft_registration = alert_key(flight_info, key_prefix)
        past_alerts = sent_alerts.loc[sent_alerts['flight_num'] == aircraft_registration]

        if len(past_alerts) >= settings['max_alerts_per_aircraft']:
            continue

        # Repeat notifications must wait realert_minutes after the previous one.
        if len(past_alerts) > 0:
            elapsed_minutes = (current_timestamp - past_alerts['sent_time'].max()) / 60
            if elapsed_minutes <= settings['realert_minutes']:
                continue

        flight_info = dict(flight_info, aircraft_registration=aircraft_registration)
        eligible_flights.append(flight_info)
    return eligible_flights


def run_check(settings):
    """One polling cycle. Returns a summary dict; raises only on unexpected errors."""
    result = {
        'started_at': time.time(),
        'ok': False,
        'error': None,
        'aircraft_total': 0,
        'aircraft_positioned': 0,
        'military_seen': [],
        'military_in_range': [],
        'watch_in_range': [],
        'notified': [],
        'emails_sent': 0,
        'email_sent': False,
    }

    if not settings['aircraft_data_url']:
        result['error'] = 'Receiver URL is not set'
        return result

    json_dump = fetch_aircraft_data(settings['aircraft_data_url'])
    if json_dump is None:
        result['error'] = 'Could not reach the receiver'
        return result

    home_coords = (settings['home_lat'], settings['home_lon'])
    aircraft_db_url = get_aircraft_db_url(settings['aircraft_data_url'])
    aircraft = json_dump.get('aircraft', [])
    result['aircraft_total'] = len(aircraft)

    # Aircraft outside every alert radius can't trigger an email, so they don't need a database lookup.
    lookup_radius = max([settings['alert_radius_miles']]
                        + [entry['radius_miles'] for entry in settings['watchlist'] if entry['enabled']])

    # (entry, [flights]) for each watchlist entry with a matching aircraft inside its own radius
    watch_hits = {}
    for flight in aircraft:
        if flight.get('seen', 999) >= settings['max_seen_seconds']:
            continue
        if 'lat' not in flight or 'lon' not in flight:
            continue
        result['aircraft_positioned'] += 1

        annotate_aircraft(flight, home_coords, aircraft_db_url, settings['watchlist'], lookup_radius)
        for entry in flight['watch_entries']:
            if flight['distance'] <= entry['radius_miles']:
                watch_hits.setdefault(entry['id'], (entry, []))[1].append(flight)
                result['watch_in_range'].append(flight)

        if flight['military']:
            result['military_seen'].append(flight)
            if flight['distance'] <= settings['alert_radius_miles']:
                result['military_in_range'].append(flight)

    # Each batch is one email: all military aircraft together, then one per watchlist entry.
    batches = [(settings['subject'], settings['distribution_list'], 'Military aircraft within range', '',
                result['military_in_range'])]
    for entry, flights in watch_hits.values():
        subject = entry['subject'] or (watch_label(entry) + ' is within ' + str(entry['radius_miles']) + ' mi')
        batches.append((subject, entry['recipients'] or settings['distribution_list'],
                        watch_label(entry) + ' within range', 'watch:' + entry['id'] + ':', flights))

    with SENT_ALERTS_LOCK:
        sent_alerts = load_sent_alerts()
        for subject, recipients, heading, key_prefix, flights in batches:
            eligible_flights = select_eligible_flights(flights, sent_alerts, settings, time.time(), key_prefix)
            if not eligible_flights:
                continue
            result['notified'].extend(eligible_flights)
            if not settings['send_emails']:
                # Not logged as sent, so they alert once emails are switched back on.
                logger.info('Emails are off; would have sent "' + subject + '" about ' + str(len(eligible_flights)) + ' aircraft.')
                continue
            send_email(eligible_flights, subject, recipients, heading, settings)
            sent_alerts = write_alert_log(eligible_flights, sent_alerts)
            save_sent_alerts(sent_alerts)
            result['emails_sent'] += 1

    result['email_sent'] = result['emails_sent'] > 0
    if not result['notified']:
        logger.info('Checked ' + str(result['aircraft_positioned']) + ' aircraft; '
                    + str(len(result['military_in_range'])) + ' military and '
                    + str(len(result['watch_in_range'])) + ' watched in range, none due a notification.')
    result['ok'] = True
    return result


def send_message(subject, message_text, recipients, smtp):
    if not smtp['configured']:
        raise RuntimeError('The email server is not set up. Add the SMTP server, username and password in Settings.')

    msg = MIMEText(message_text, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = formataddr((str(Header(smtp['from_name'], 'utf-8')), smtp['username']))
    msg['To'] = ', '.join(recipients)

    # create_default_context() verifies the server certificate; smtplib's own default does not.
    context = ssl.create_default_context()
    if smtp['security'] == 'starttls':
        server = smtplib.SMTP(smtp['hostname'], smtp['port'], timeout=30)
        server.starttls(context=context)
    else:
        server = smtplib.SMTP_SSL(smtp['hostname'], smtp['port'], timeout=30, context=context)
    try:
        server.login(smtp['username'], smtp['password'])
        server.sendmail(smtp['username'], recipients, msg.as_string())
    finally:
        server.quit()


def send_email(flights, subject, recipients, heading, settings):
    flights_sorted = sorted(flights, key=lambda item: item.get('distance', 9999))
    message_lines = [
        heading + ': ' + str(len(flights_sorted)),
        ''
    ]

    for index, flight_info in enumerate(flights_sorted, start=1):
        message_lines.append('Aircraft #' + str(index))
        message_lines.append('Distance: ' + str(flight_info.get('distance', 'unknown')) + ' miles')
        message_lines.append('Type: ' + get_aircraft_type(flight_info))
        message_lines.append('Flight: ' + str(flight_info.get('flight', 'unknown')))
        message_lines.append('Registration: ' + str(flight_info.get('r', 'unknown')))
        message_lines.append('Hex: ' + str(flight_info.get('hex', 'unknown')))
        message_lines.append('All Stats:')
        for key in sorted(flight_info.keys()):
            if key in ('watch_entries', 'aircraft_registration'):
                continue
            message_lines.append('  ' + str(key) + ': ' + str(flight_info.get(key)))
        message_lines.append('')

    full_subject = subject + ' (' + str(len(flights_sorted)) + ')'
    send_message(full_subject, '\r\n'.join(message_lines), recipients, smtp_config(settings))
    logger.info('Email "' + full_subject + '" sent to ' + str(len(recipients)) + ' recipient(s)')


def send_test_email(settings):
    send_message(settings['subject'] + ' (test)',
                 'This is a test message from Flight Alerter. If you can read this, email alerts are working.',
                 settings['distribution_list'], smtp_config(settings))
    logger.info('Test email sent to ' + ', '.join(settings['distribution_list']))


def write_alert_log(flights, sent_alerts):
    timestamp = datetime.timestamp(datetime.now())

    new_rows = []
    for flight_info in flights:
        new_rows.append({
            'flight_num': flight_info.get('aircraft_registration', 'unknown'),
            'flight_name': str(flight_info.get('flight') or '').strip() or flight_info.get('r') or 'unknown',
            'sent_time': timestamp
        })

    if new_rows:
        sent_alerts = pd.concat([sent_alerts, pd.DataFrame(new_rows)], ignore_index=True)

    return sent_alerts


def main():
    """Run a single check from the command line. The web app (app.py) runs this on a loop."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    result = run_check(config.load_settings())
    if not result['ok']:
        logger.error(result['error'])


if __name__ == "__main__":
    main()
