# Weekly forward-clock scheduler (macOS launchd)

Runs one `src.forward.run` session per week, hands-off. Laptop-first: it fires only
while you're logged in, and if the Mac is asleep at the scheduled time launchd runs it
once on the next wake.

Pieces:
- `scripts/forward_weekly.sh` — the wrapper. Runs the session, full output to a dated
  log under `data/forward/logs/`, a one-line status to stdout. Configurable via env:
  `PARLEY_SOURCE` (ibkr|fmp), `PARLEY_MAX_LLM_USD`, `PARLEY_ARGS`.
- `scripts/com.parley.forward-weekly.plist` — the LaunchAgent. Default: **Sunday 17:00**,
  `PARLEY_SOURCE=ibkr`, cap `$8`. Edit `Weekday`/`Hour`/`Minute` and the `PARLEY_*` vars.

## Choose the source — the one real decision

- **`fmp` (recommended for the unattended schedule).** No broker dependency, so the
  weekly tick *never silently misses*. The paper book is simulated, so this is a fully
  faithful weekly record.
- **`ibkr`.** Real-broker data, but **IB Gateway must be running and logged in at the
  scheduled time** — and Gateway force-logs-out daily. So a hands-off `ibkr` schedule
  needs either (a) you ensuring Gateway is up before the run, or (b) IBC to keep it
  logged in headless. Otherwise the run fails (logged, not silent) and the week is lost.

Practical setup: **schedule on `fmp`** so the clock is reliable, and run `ibkr` **by
hand** when you want to exercise the real connection (`PARLEY_SOURCE=ibkr
scripts/forward_weekly.sh`). Switch the schedule to `ibkr` once IBC (or your routine)
guarantees Gateway is up.

## Install

```sh
cp scripts/com.parley.forward-weekly.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.parley.forward-weekly.plist
# (older macOS: launchctl load -w ~/Library/LaunchAgents/com.parley.forward-weekly.plist)
launchctl print gui/$(id -u)/com.parley.forward-weekly   # verify it's loaded
```

After editing the plist later, reload: `bootout` then `bootstrap` again.

## Test / operate

```sh
# Run it once right now, by hand (uses the plist's schedule-independent logic):
PARLEY_SOURCE=fmp PARLEY_MAX_LLM_USD=5 scripts/forward_weekly.sh

# Fire the scheduled job immediately (tests the launchd wiring too):
launchctl kickstart -k gui/$(id -u)/com.parley.forward-weekly

# See what happened:
ls -t data/forward/digests/ | head        # this week's brief
tail -n 40 data/forward/logs/forward_*.log # full run log
```

## Uninstall

```sh
launchctl bootout gui/$(id -u)/com.parley.forward-weekly
rm ~/Library/LaunchAgents/com.parley.forward-weekly.plist
```
