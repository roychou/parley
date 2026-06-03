# Weekly forward-clock scheduler (macOS launchd)

Runs one `src.forward.run` session per week, hands-off. Laptop-first: it fires only
while you're logged in, and if the Mac is asleep at the scheduled time launchd runs it
once on the next wake.

Pieces:
- `scripts/forward_weekly.sh` — the wrapper. Runs the session, full output to a dated
  log under `data/forward/logs/`, a one-line status to stdout. Configurable via env:
  `PARLEY_MAX_LLM_USD`, `PARLEY_ARGS`.
- `scripts/com.parley.forward-weekly.plist` — the LaunchAgent. Default: **Sunday 17:00**,
  cap `$8`. Edit `Weekday`/`Hour`/`Minute` and the `PARLEY_*` vars.

## Gateway must be up

Prices + Benzinga news come from IB Gateway/TWS (fundamentals are EDGAR, free) — there
is no broker-free data source. **IB Gateway must be running and logged in at the
scheduled time**, and Gateway force-logs-out daily, so a hands-off schedule needs
either (a) you ensuring Gateway is up before the run, or (b) IBC to keep it logged in
headless. If Gateway is down the run fails loudly (logged, not silent) and the week is
lost — re-run by hand once Gateway is back (`scripts/forward_weekly.sh`).

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
PARLEY_MAX_LLM_USD=5 scripts/forward_weekly.sh

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
