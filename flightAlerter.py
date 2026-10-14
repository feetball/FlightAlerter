import geopy.distance
import pandas as pd
import requests
import smtplib
import time
import os
import re

from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import datetime
from os.path import exists


AIRCRAFT_DB_CACHE = {}


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
            print('Aircraft data fetch failed (attempt ' + str(attempt) + '/' + str(max_attempts) + '): ' + str(exc))
            if attempt < max_attempts:
                print('Retrying in ' + str(retry_delay_seconds) + 's...')
                time.sleep(retry_delay_seconds)
        except ValueError as exc:
            print('Aircraft data response was not valid JSON: ' + str(exc))
            return None

    print('Unable to fetch aircraft data after retries. Skipping this cycle.')
    return None

def main():
    try:
        send_emails = True
        smtp_hostname = os.environ.get('SMTP_HOSTNAME', 'smtp.zoho.com')
        smtp_port = int(os.environ.get('SMTP_PORT', '465'))
        smtp_username = os.environ['SMTP_USERNAME']
        smtp_password = os.environ['SMTP_PASSWORD']

        home_coords = (float(os.environ['HOME_LAT']), float(os.environ['HOME_LON']))

        sent_alerts_filename = './sentAlerts.csv'
        distribution_list = [smtp_username]
        alert_radius_miles = 25
        subject = 'Military aircraft nearby'

        config_filename = None
        if exists('./militaryFlights.csv'):
            config_filename = './militaryFlights.csv'
        elif exists('./monitoredFlits.csv'):
            config_filename = './monitoredFlits.csv'
        elif exists('./monitoredFlights.csv'):
            config_filename = './monitoredFlights.csv'

        if config_filename:
            config = pd.read_csv(config_filename)
            if len(config) > 0:
                config_row = config.iloc[0]

                distance_value = None
                if 'military_distance' in config.columns and pd.notna(config_row.get('military_distance')):
                    distance_value = config_row.get('military_distance')
                elif 'max_distance' in config.columns and pd.notna(config_row.get('max_distance')):
                    distance_value = config_row.get('max_distance')

                if distance_value is not None:
                    alert_radius_miles = float(distance_value)

                if 'distribution_list' in config.columns and pd.notna(config_row.get('distribution_list')):
                    distribution_list = str(config_row.get('distribution_list')).split('|')

                if 'subject' in config.columns and pd.notna(config_row.get('subject')):
                    subject = str(config_row.get('subject'))
        
        print('Current Path: ')
        print(os.path.realpath(__file__))
        print('Military alert radius: ' + str(alert_radius_miles) + 'mi')
        
        if exists(sent_alerts_filename):
            sent_alerts = pd.read_csv(sent_alerts_filename)
        else:
            sent_alerts = pd.DataFrame(columns=['flight_num', 'flight_name', 'sent_time'])

        aircraft_data_url = os.environ['AIRCRAFT_DATA_URL']
        aircraft_db_url = get_aircraft_db_url(aircraft_data_url)

        # get json data
        json_dump = fetch_aircraft_data(aircraft_data_url)
        if json_dump is None:
            return

        military_flights_in_range = []

        for flight in json_dump.get('aircraft', []):
            if flight.get('seen', 999) >= 10:
                continue
            if 'lat' not in flight or 'lon' not in flight:
                continue
            if not is_military_aircraft(flight):
                continue

            flight_info = flight
            flight_coords = (flight_info['lat'], flight_info['lon'])
            flight_info['distance'] = round(geopy.distance.distance(home_coords, flight_coords).mi, 1)

            if flight_info['distance'] > alert_radius_miles:
                continue

            military_flights_in_range.append(flight_info)

        eligible_flights = []
        current_timestamp = datetime.timestamp(datetime.now())

        for flight_info in military_flights_in_range:
            aircraft_registration = str(flight_info.get('hex') or flight_info.get('flight') or 'unknown').strip().lower()
            if not aircraft_registration:
                aircraft_registration = 'unknown'

            past_alerts = sent_alerts.loc[sent_alerts['flight_num'] == aircraft_registration]

            # Only allow two total notifications for an aircraft.
            if len(past_alerts) >= 2:
                continue

            # The second notification can only be sent 30 minutes after the first.
            if len(past_alerts) == 1:
                alert_timestamp = past_alerts.iloc[-1]['sent_time']
                elapsed_time = (current_timestamp - alert_timestamp) / 60
                if elapsed_time <= 30:
                    continue

            flight_info = enrich_flight_with_aircraft_db(flight_info, aircraft_db_url)
            flight_info['aircraft_registration'] = aircraft_registration
            eligible_flights.append(flight_info)

        if len(eligible_flights) > 0:
            sent_alerts = send_email(eligible_flights, sent_alerts, send_emails, smtp_password, smtp_port, smtp_username, smtp_hostname, distribution_list, subject)
        else:
            print('No eligible military aircraft to notify.')

        # write sent_alerts to file
        sent_alerts.to_csv(sent_alerts_filename, index=False)
        print('Flight Alerter ran at: ' + datetime.now().strftime("%m/%d/%Y, %H:%M:%S"))
        
    except Exception as e:
        print('Something went wrong...')
        print(repr(e))
    finally:
        print('Sleeping for 1min')
        time.sleep(60)

def send_email(flights, sent_alerts, send_emails, smtp_password, smtp_port, smtp_username, smtp_hostname, distribution_list, subject):
    if send_emails:
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

        message_text = '\r\n'.join(message_lines)
        msg = MIMEText(message_text, 'plain', 'utf-8')
        msg['Subject'] =  Header(subject + ' (' + str(len(flights_sorted)) + ')', 'utf-8')
        msg['From'] = formataddr((str(Header('Daniel Kuecker', 'utf-8')), smtp_username))
        msg['To'] = ', '.join(distribution_list)
        
        # Create server object with SSL option
        server = smtplib.SMTP_SSL(smtp_hostname, smtp_port)
        
        server.login(smtp_username, smtp_password)
        server.sendmail(smtp_username, distribution_list, msg.as_string())
        server.quit()
        
        print('Summary email sent for ' + str(len(flights_sorted)) + ' military aircraft')
    return write_alert_log(flights, sent_alerts) 

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
    
     
if __name__ == "__main__":
    main()
