# flightAlerter

Sends an email when a military aircraft flies within a set radius of your location.
It reads live positions from your own ADS-B receiver.

Every minute it fetches `aircraft.json` from a SkyAware / tar1090 / readsb receiver.
It keeps aircraft that look military and are inside the alert radius, then sends one
summary email listing them by distance. Each email includes the aircraft type, which
is looked up in the receiver's aircraft database.

## Requirements

- An ADS-B receiver that serves `.../data/aircraft.json` and the matching `.../db/`
  aircraft database (SkyAware and tar1090 both do)
- An SMTP account that can send mail over SSL (an app password is recommended)
- Docker and docker-compose, or Python 3 to run it without Docker

## Setup

1. Copy the example environment file and fill it in:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

   | Variable            | Description                                                   |
   |---------------------|---------------------------------------------------------------|
   | `SMTP_HOSTNAME`     | SMTP server (default `smtp.zoho.com`)                         |
   | `SMTP_PORT`         | SMTP SSL port (default `465`)                                 |
   | `SMTP_USERNAME`     | Login and From address; also the default recipient           |
   | `SMTP_PASSWORD`     | SMTP password or app password                                 |
   | `HOME_LAT`          | Latitude that distances are measured from                     |
   | `HOME_LON`          | Longitude that distances are measured from                    |
   | `AIRCRAFT_DATA_URL` | Full URL to the receiver's `aircraft.json`                     |

   `.env` is git-ignored and excluded from the Docker image. Never commit it.

2. Optionally, edit `militaryFlights.csv` to change who gets alerts and the radius:

   ```csv
   distribution_list,military_distance,subject
   you@example.com|friend@example.com,25,Military aircraft nearby
   ```

   - `distribution_list`: recipients, separated by `|`
   - `military_distance`: alert radius in miles (default 25)
   - `subject`: email subject; the aircraft count is appended

   Only the first row is used. If the file is missing, the script falls back to
   `monitoredFlights.csv`, then to the defaults.

## Running

With Docker:

```sh
docker-compose up -d --build
```

The script runs one check, sleeps 60 seconds and exits. The `restart: unless-stopped`
policy then starts it again, so it checks about once a minute.

Without Docker:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
venv/bin/python flightAlerter.py
```

## How aircraft are classified as military

An aircraft counts as military if any of these is true:

- The receiver database marks it military (`dbFlags` bit 1, or `military: true`)
- Its operator field contains words such as `AIR FORCE`, `USAF`, `NAVY`, `ARMY` or
  `COAST GUARD`
- Its callsign matches a known military prefix (`RCH`, `REACH`, `PAT`, `SPAR`, and others)

Aircraft not seen in the last 10 seconds, or with no position, are skipped.

## Alert limits

Sent alerts are logged in `sentAlerts.csv`, keyed by ICAO hex code. Each aircraft
triggers at most two emails, and the second only comes at least 30 minutes after the
first. Delete rows from that file to reset an aircraft.
