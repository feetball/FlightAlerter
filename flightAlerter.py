import geopy.distance
import logging
import pandas as pd
import requests
import smtplib
import threading
import time
import os
import re

from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime
from os.path import exists

import settings as config


logger = logging.getLogger('flightAlerter')

AIRCRAFT_DB_CACHE = {}
SENT_ALERTS_FILENAME = './sentAlerts.csv'
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

        try:
            response = requests.get(db_url + '/' + block_key + '.json', timeout=timeout_seconds)
            if response.status_code == 404:
                AIRCRAFT_DB_CACHE[icao] = {}
                return {}

            response.raise_for_status()
            shard_data = response.json()
        except (requests.exceptions.RequestException, ValueError):
            AIRCRAFT_DB_CACHE[icao] = {}
            return {}

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


def smtp_config():
    """SMTP credentials come only from the environment (.env), never from settings.json."""
    username = os.environ.get('SMTP_USERNAME', '')
    password = os.environ.get('SMTP_PASSWORD', '')
    return {
        'hostname': os.environ.get('SMTP_HOSTNAME', 'smtp.zoho.com'),
        'port': int(os.environ.get('SMTP_PORT', '465')),
        'username': username,
        'password': password,
        'from_name': os.environ.get('SMTP_FROM_NAME', 'Flight Alerter'),
        'configured': bool(username and password),
    }


def annotate_aircraft(flight, home_coords):
    flight['distance'] = round(geopy.distance.distance(home_coords, (flight['lat'], flight['lon'])).mi, 1)
    flight['military'] = is_military_aircraft(flight)
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
        'seen': flight.get('seen'),
    }


def load_sent_alerts():
    if exists(SENT_ALERTS_FILENAME):
        return pd.read_csv(SENT_ALERTS_FILENAME, dtype={'flight_num': str, 'flight_name': str})
    return pd.DataFrame(columns=SENT_ALERT_COLUMNS)


def save_sent_alerts(sent_alerts):
    sent_alerts.to_csv(SENT_ALERTS_FILENAME, index=False)


def alert_key(flight):
    return str(flight.get('hex') or flight.get('flight') or 'unknown').strip().lower() or 'unknown'


def select_eligible_flights(flights, sent_alerts, settings, current_timestamp):
    eligible_flights = []
    for flight_info in flights:
        aircraft_registration = alert_key(flight_info)
        past_alerts = sent_alerts.loc[sent_alerts['flight_num'] == aircraft_registration]

        if len(past_alerts) >= settings['max_alerts_per_aircraft']:
            continue

        # Repeat notifications must wait realert_minutes after the previous one.
        if len(past_alerts) > 0:
            elapsed_minutes = (current_timestamp - past_alerts['sent_time'].max()) / 60
            if elapsed_minutes <= settings['realert_minutes']:
                continue

        flight_info['aircraft_registration'] = aircraft_registration
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
        'notified': [],
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

    military_in_range = []
    for flight in aircraft:
        if flight.get('seen', 999) >= settings['max_seen_seconds']:
            continue
        if 'lat' not in flight or 'lon' not in flight:
            continue
        result['aircraft_positioned'] += 1

        annotate_aircraft(flight, home_coords)
        if not flight['military']:
            continue

        result['military_seen'].append(flight)
        if flight['distance'] <= settings['alert_radius_miles']:
            military_in_range.append(flight)

    for flight in result['military_seen']:
        enrich_flight_with_aircraft_db(flight, aircraft_db_url)
    result['military_in_range'] = military_in_range

    with SENT_ALERTS_LOCK:
        sent_alerts = load_sent_alerts()
        eligible_flights = select_eligible_flights(military_in_range, sent_alerts, settings, time.time())

        if not eligible_flights:
            logger.info('Checked ' + str(result['aircraft_positioned']) + ' aircraft; '
                        + str(len(military_in_range)) + ' military in range, none due a notification.')
        elif not settings['send_emails']:
            # Not logged as sent, so they alert once emails are switched back on.
            logger.info('Emails are off; would have notified about ' + str(len(eligible_flights)) + ' aircraft.')
        else:
            send_email(eligible_flights, settings, smtp_config())
            save_sent_alerts(write_alert_log(eligible_flights, sent_alerts))
            result['email_sent'] = True
        result['notified'] = eligible_flights

    result['ok'] = True
    return result


def send_message(subject, message_text, recipients, smtp):
    if not smtp['configured']:
        raise RuntimeError('SMTP_USERNAME and SMTP_PASSWORD are not set in .env')

    msg = MIMEText(message_text, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = formataddr((str(Header(smtp['from_name'], 'utf-8')), smtp['username']))
    msg['To'] = ', '.join(recipients)

    server = smtplib.SMTP_SSL(smtp['hostname'], smtp['port'], timeout=30)
    try:
        server.login(smtp['username'], smtp['password'])
        server.sendmail(smtp['username'], recipients, msg.as_string())
    finally:
        server.quit()


def send_email(flights, settings, smtp):
    flights_sorted = sorted(flights, key=lambda item: item.get('distance', 9999))
    message_lines = [
        'Military aircraft within range: ' + str(len(flights_sorted)),
        ''
    ]

    for index, flight_info in enumerate(flights_sorted, start=1):
        message_lines.append('Aircraft #' + str(index))
        message_lines.append('Distance: ' + str(flight_info.get('distance', 'unknown')) + ' miles')
        message_lines.append('Type: ' + get_aircraft_type(flight_info))
        message_lines.append('Flight: ' + str(flight_info.get('flight', 'unknown')))
        message_lines.append('Hex: ' + str(flight_info.get('hex', 'unknown')))
        message_lines.append('Registration Key: ' + str(flight_info.get('aircraft_registration', 'unknown')))
        message_lines.append('All Stats:')
        for key in sorted(flight_info.keys()):
            message_lines.append('  ' + str(key) + ': ' + str(flight_info.get(key)))
        message_lines.append('')

    subject = settings['subject'] + ' (' + str(len(flights_sorted)) + ')'
    send_message(subject, '\r\n'.join(message_lines), settings['distribution_list'], smtp)
    logger.info('Summary email sent for ' + str(len(flights_sorted)) + ' military aircraft')


def send_test_email(settings):
    send_message(settings['subject'] + ' (test)',
                 'This is a test message from Flight Alerter. If you can read this, email alerts are working.',
                 settings['distribution_list'], smtp_config())
    logger.info('Test email sent to ' + ', '.join(settings['distribution_list']))


def write_alert_log(flights, sent_alerts):
    timestamp = datetime.timestamp(datetime.now())

    new_rows = []
    for flight_info in flights:
        new_rows.append({
            'flight_num': flight_info.get('aircraft_registration', 'unknown'),
            'flight_name': flight_info.get('flight', 'unknown'),
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
