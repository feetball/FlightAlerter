# flightAlerter

Sends an email when a military aircraft, or an aircraft on your watchlist, flies near
your location. It reads live positions from your own ADS-B receiver.

Every minute it fetches `aircraft.json` from a SkyAware / tar1090 / readsb receiver and
sends email for:

- **Military aircraft** within the alert radius, in one summary email listing them by
  distance
- **Watchlist aircraft** within their own radius. Each entry has its own recipients and
  subject, and can be matched by tail number, callsign or ICAO hex code.

Aircraft type and registration are looked up in the receiver's aircraft database. That
means a tail number on the watchlist is recognised even when the aircraft isn't
broadcasting it as its callsign.

A password-protected web dashboard (port 8095) shows:

- a live map of traffic, with military and watchlist aircraft highlighted and the alert
  radius drawn
- status: last and next check, aircraft counts, last email, recent errors
- the history of sent alerts, with a button to reset any aircraft
- settings for everything, including the watchlist and the email server
- an activity log

## Requirements

- An ADS-B receiver that serves `.../data/aircraft.json` and the matching `.../db/`
  aircraft database (SkyAware and tar1090 both do)
- An SMTP account that can send mail over SSL/TLS or STARTTLS (an app password is
  recommended)
- Docker and docker-compose, or Python 3 to run it without Docker

## Setup

1. Optionally, create a `.env` file from the example to pre-fill settings:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

   These values are only used on the first start. You can skip this and enter
   everything in the dashboard instead. Once the service has started, you can remove
   the SMTP password from `.env`.

2. Start the service (see below) and open `http://<host>:8095`.

3. On first visit you'll be asked to create an admin password. To prove you run the
   server, enter the one-time setup code printed in its log:

   ```sh
   docker logs flightalerter-flightalerter-1 | grep "setup code"
   ```

   If `WEB_PASSWORD` is set in `.env`, it becomes the initial password and no setup
   code is needed.

4. Fill in the **Settings** page: receiver URL, home location, recipients, email server,
   watchlist. Use **Send test email** to check the email settings.

## Running

With Docker:

```sh
./deploy.sh            # build, restart, wait until the dashboard answers
./deploy.sh --pull     # git pull first
./deploy.sh --logs     # follow the logs afterwards
```

On a first install, `deploy.sh` prints the setup code for you. It is a wrapper around
`docker-compose up -d --build`, which also works on its own.

The container runs the code baked into the image. This folder is mounted at `/data`
(`DATA_DIR`), where settings, secrets and alert history are kept, so they survive
rebuilds.

Without Docker:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
```

`venv/bin/python flightAlerter.py` runs a single check without the web server, which is
handy for cron or testing.

## Using the dashboard

Open `http://<host>:8095` and sign in. The browser needs internet access for the map
(tiles come from OpenStreetMap). Everything else is served by Flight Alerter itself.

### Top bar

- **Status pill**: *Monitoring* (all good), *Checking…*, *Paused*, *Error* or *Offline*
  (the browser lost contact with the service). Next to it: when the last check ran and
  when the next one is due.
- **Monitoring** switch: pauses or resumes the regular checks straight away, without
  touching other settings. While paused, nothing is checked and no email is sent.
- **Run check**: runs a check now, outside the schedule. Any email that is due is sent,
  so this is a quick end-to-end test once the settings are filled in.
- A red banner under the bar shows the last error, for example an unreachable receiver.

### Dashboard

- **Live traffic** map, updated every few seconds. Orange is military, purple is a
  watchlist aircraft, grey-blue is everything else. The dashed circle is the military
  alert radius around your home location. Click an aircraft for its type, registration,
  hex code, distance, altitude, speed, heading and squawk. **Military & watchlist only** hides other traffic.
- **Stat cards**: aircraft tracked, military aircraft anywhere in receiver range,
  military aircraft inside the alert radius, and when the last email went out. If that
  card says *Emails off* or *Not set up*, see Settings.
- **Military aircraft** lists the military aircraft in view, nearest first. Items inside
  the radius are highlighted. Click one (or select it and press Enter) to fly the map to it.
- **Watchlist** shows each watched aircraft and whether it is in view. Click one in view
  to find it on the map. **Edit** jumps to the watchlist in Settings.
- **Traffic per check** charts aircraft counts for recent checks. Hover for details.

### Alerts

One row per aircraft that has been emailed, newest first. The row shows why it was
emailed (military or which watchlist entry), when it was last sent, and how many times.
**Reset** clears that aircraft's history so it can alert again, for example after you
raise the per-aircraft limit or want a fresh email the next time it passes.

### Settings

Changes are not applied until you press **Save settings** in the bar at the bottom.
**Discard** puts the form back to the saved values. Fields with problems are marked in
red, and nothing is saved until they are fixed.

- **Alerting**: master on/off for monitoring, **Send emails** (turn off to watch the
  dashboard without sending mail), the military alert radius, default recipients (type
  an address and press Enter or comma; click × to remove one) and the email subject.
- **Location & receiver**: the receiver's `aircraft.json` URL. **Test** checks that it
  answers and shows how many aircraft it reports. **Use receiver's location** fills in
  home latitude and longitude from the receiver, if it publishes them. Save afterwards.
- **Watchlist**: **Add aircraft**, then enter a tail number (`N251BP`), callsign
  (`SWA2713`) or ICAO hex code (`A1B2C3`). Optional nickname, radius, recipients and
  subject per entry. Blank recipients or subject use the defaults above. Untick an entry
  to pause it without deleting it.
- **Timing & limits**: how often to check, emails per aircraft, the wait between repeat
  emails, and how stale a position can be before the aircraft is ignored.
- **Email server**: SMTP host, encryption (SSL/TLS on 465 or STARTTLS on 587), port,
  username (also the From address), password and From name. The password field stays
  blank. Leave it blank to keep the saved one. **Send test email** sends to the default
  recipients using the saved settings, so save first.
- **Security**: change the admin password (signs out every other browser) or sign out.

### Log

The service's recent activity: checks, emails sent, settings changes, sign-ins and
errors. **Follow** keeps the newest line in view.

### Troubleshooting

| You see | What to do |
|---------|------------|
| *Receiver URL is not set* / *Could not reach the receiver* | Settings → Location & receiver: fix the URL, then **Test**. |
| Last email: *Not set up* | Fill in Settings → Email server, save, then **Send test email**. |
| An aircraft in range but no email | Check the Alerts page: it may have hit its email limit or still be in the wait between repeats. **Reset** it there. Also check that **Send emails** is on. |
| Map tiles say *Access blocked* | Hard-refresh the page (Ctrl+Shift+R). OpenStreetMap refuses some requests; see its tile usage policy. |
| *Offline* banner | The service isn't running: `./deploy.sh` or `docker logs flightalerter-flightalerter-1`. |

## Files

| File               | Contents                                                    | In git? |
|--------------------|-------------------------------------------------------------|---------|
| `settings.json`    | All settings except passwords                               | No      |
| `secrets.json`     | SMTP password, admin password hash, session signing key     | No      |
| `sentAlerts.csv`   | Which aircraft have been emailed, and when                  | No      |
| `.env`             | Optional first-start values (see `.env.example`)            | No      |

`militaryFlights.csv` and `monitoredFlights.csv` are the old config files. If they
exist on first start, their values are imported into `settings.json` and the
watchlist. After that they are ignored.

## Security

- **Login.** Every page and API call needs a login. The admin password is stored only
  as a salted PBKDF2 hash. Sessions use an HttpOnly, SameSite=Strict cookie and last
  30 days.
- **Changing the password** signs out every other browser. Signing out only clears the
  cookie in that browser, so change the password if you think a session was stolen.
- **Wrong passwords.** After 5 wrong attempts, logins from that address are refused
  for 15 minutes.
- **Forged requests.** Every request that changes something must carry a custom header,
  which blocks cross-site request forgery. Pages are sent with a strict Content Security
  Policy and can't be embedded in frames.
- **SMTP password.** It's write-only in the UI: it is never sent back to the browser or
  written to the log. It's stored in plain text in `secrets.json`, which only its owner
  can read, as `.env` was before.
- **Certificates.** Email is sent over TLS, and the mail server's certificate is
  verified.
- **HTTPS.** The dashboard itself speaks plain HTTP. Keep it on your LAN. To reach it
  from outside, put it behind a reverse proxy with HTTPS and set `WEB_HTTPS=1` so the
  session cookie is only sent over HTTPS.

**Forgot the admin password?** Delete the `admin_password_hash` line from
`secrets.json` and restart. A new setup code will be printed in the log.

## How aircraft are classified as military

An aircraft counts as military if any of these is true:

- The receiver database marks it military (`dbFlags` bit 1, or `military: true`)
- Its operator field contains words such as `AIR FORCE`, `USAF`, `NAVY`, `ARMY` or
  `COAST GUARD`
- Its callsign matches a known military prefix (`RCH`, `REACH`, `PAT`, `SPAR`, and others)

The database lookup is skipped for aircraft outside every alert radius (they can't
trigger an email anyway), so those are classified from the receiver's own data only.

Aircraft not heard from in the last 10 seconds (configurable), or with no position,
are skipped.

## Alert limits

Sent alerts are logged in `sentAlerts.csv`. By default each aircraft triggers at most
two emails, at least 30 minutes apart; both limits can be changed in Settings. Military
and watchlist alerts are counted separately. To let an aircraft alert again, reset it on
the **Alerts** page.

While **Send emails** is off, nothing is recorded, so aircraft still in range alert
once emails are turned back on.
