# flightAlerter

Sends an email when a military aircraft flies within a set radius of your location.
It reads live positions from your own ADS-B receiver.

Every minute it fetches `aircraft.json` from a SkyAware / tar1090 / readsb receiver.
It keeps aircraft that look military and are inside the alert radius, then sends one
summary email listing them by distance. Each email includes the aircraft type, which
is looked up in the receiver's aircraft database.

It also serves a web dashboard (port 8095) with:

- a live map of traffic, with military aircraft highlighted and the alert radius drawn
- status: last and next check, aircraft counts, last email, recent errors
- the history of sent alerts, with a button to reset any aircraft
- a settings page for the radius, recipients, home location, receiver URL and timing
- an activity log

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
   | `SMTP_FROM_NAME`    | Display name on outgoing email (default `Flight Alerter`)     |
   | `WEB_PASSWORD`      | Optional. If set, the dashboard asks for this password        |
   | `WEB_PORT`          | Optional. Dashboard port (default `8095`)                      |

   `.env` is git-ignored and excluded from the Docker image. Never commit it.

2. Start it (see below) and open `http://<host>:8095`. Everything except the SMTP login
   can be changed on the **Settings** page.

### Where settings live

Settings are saved to `settings.json`, which is git-ignored. On first start it is
created from `HOME_LAT`, `HOME_LON` and `AIRCRAFT_DATA_URL` in `.env`, and from
`militaryFlights.csv` if that file exists. After that, `settings.json` takes priority, so
change those values on the Settings page rather than in `.env`.

The SMTP username and password are only ever read from `.env`. The dashboard never
shows or stores the password.

## Running

With Docker:

```sh
docker-compose up -d --build
```

The dashboard is then at `http://<host>:8095`. The container runs the code from this
folder (mounted at `/code`), so `settings.json` and `sentAlerts.csv` survive rebuilds.

Without Docker:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
venv/bin/python app.py
```

`venv/bin/python flightAlerter.py` runs a single check without the web server, which is
handy for cron or testing.

### Security

The dashboard has no login unless `WEB_PASSWORD` is set. Anyone who can reach the port
can change settings, pause alerts and send test emails. Set a password, or keep the
port off the internet.

## How aircraft are classified as military

An aircraft counts as military if any of these is true:

- The receiver database marks it military (`dbFlags` bit 1, or `military: true`)
- Its operator field contains words such as `AIR FORCE`, `USAF`, `NAVY`, `ARMY` or
  `COAST GUARD`
- Its callsign matches a known military prefix (`RCH`, `REACH`, `PAT`, `SPAR`, and others)

Aircraft not heard from in the last 10 seconds (configurable), or with no position,
are skipped.

## Alert limits

Sent alerts are logged in `sentAlerts.csv`, keyed by ICAO hex code. By default each
aircraft triggers at most two emails, at least 30 minutes apart; both limits can be
changed in Settings. To let an aircraft alert again, reset it on the **Alerts** page.

While **Send emails** is off, nothing is recorded, so aircraft still in range alert
once emails are turned back on.
