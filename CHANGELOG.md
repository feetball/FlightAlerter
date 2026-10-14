# Changelog

Versions follow [Semantic Versioning](https://semver.org): the number is in the `VERSION`
file and shown next to the name in the dashboard.

## 1.0.0

First tagged release.

### Added
- Several aircraft in one watchlist entry (`N324FX, N268FX, …`, up to 50), with
  `<aircraft>` and `<radius>` filled in to email subjects.
- Dashboard with live map, alerts history, settings and log; password login.
- *Stalled* status when checks stop running, and a banner for emails that fail to send.
- Unit tests (`python -m unittest discover -s tests -t .`).
- Version number in the dashboard, `/api/status` and the startup log.

### Changed
- Settings, secrets and alert history live in `data/` (or `DATA_PATH`). `deploy.sh` moves
  files from older installs.
- The container runs as a non-root user on a pinned `python:3.13-slim` base with pinned
  dependencies; pandas was dropped (image 1.3 GB to 128 MB).
- Only positions newer than "Ignore positions older than" (default 10 s) trigger emails.
- Alert history is saved atomically and tolerates damaged lines.
- Checks report "Home location is not set" instead of silently never alerting.

### Fixed
- Operator names match whole words, so `AIRCRAFT GUARANTY CORP` is no longer military.
- One failed email no longer blocks the rest; it is retried at the next check.
- A scheduler error no longer stops monitoring for good.
- Dashboard polling no longer piles up requests, and pauses in background tabs.
- Login throttling can't be bypassed with parallel requests.
- Fresh clones no longer inherit the author's email address.
