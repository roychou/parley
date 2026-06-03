# Deployment — forward clock off the laptop (Docker)

Runs the weekly forward paper session on an always-on host instead of the laptop, so a
missed run never silently punches a hole in the GATE-0 evidence record. Two containers,
hand-rolled (no third-party images), on a private network:

- **`gateway`** — IB Gateway + IBC (paper), headless via Xvfb. Holds the broker session;
  IBC re-logs-in across IBKR's daily restart. API relayed off localhost by socat.
- **`app`** — the parley scheduler (`supercronic`): the weekly session + a daily
  staleness watchdog. Connects to `gateway:4004`; fundamentals come from EDGAR (free).

Files: [deploy/docker-compose.yml](../deploy/docker-compose.yml),
[deploy/gateway/](../deploy/gateway/), [deploy/app/](../deploy/app/),
[deploy/.env.example](../deploy/.env.example).

## ⚠️ Read first — what is NOT verified

This stack was authored where it could not be built or run (no Docker daemon, no network
to IBKR's installer, no credentials). The Python reliability layer (preflight, email,
heartbeat) **is** unit-tested; the **containers are not**. Expect to confirm/tweak on the
first `docker compose build`, in rough order of likelihood:

1. **IB Gateway installer flags** — `deploy/gateway/Dockerfile` uses `-q -dir`. If the
   build hangs or installs to the wrong place, try `--mode unattended --prefix ...`.
2. **IBC version + launcher** — pin a current release in `IBC_VERSION`; confirm
   `gatewaystart.sh --gateway` is the right entry for that version (env contract is set
   in `entrypoint.sh`). Some versions want the TWS major version as an argument.
3. **Gateway settings persistence** — the "auto restart" (not auto-logoff) setting and
   the API config live in `jts.ini`; the `gateway_settings` volume (`/root/Jts`) persists
   them, but confirm the path matches where the standalone Gateway actually writes.
4. **2FA** — paper logins generally don't prompt 2FA; if yours does, headless login
   can't satisfy it and you'll need IBKR's settings to disable it for the paper login.
5. **Dividend seed** — see "State" below (optional; absent just means no div crediting).

Treat a Gateway/IBC version bump as a deploy, like a model-ID bump.

## Prerequisites

- A small always-on Linux host with Docker + Docker Compose v2 (a $5–10/mo VPS is plenty;
  the job runs minutes/week). Pick a region with decent latency to IBKR.
- IBKR **paper** account credentials + a US **market-data subscription** (~$10/mo) on it,
  or price bars come back empty.
- An SMTP sender for alerts (Gmail/Fastmail app password is simplest; Proton needs Bridge).

## Setup

```sh
# on the host
git clone <your repo> parley && cd parley/deploy
cp .env.example .env && $EDITOR .env      # IBKR paper creds, Anthropic key, SMTP, TZ
docker compose build                       # first build pulls Gateway + IBC + deps
docker compose up -d
docker compose logs -f gateway             # watch IBC log in; wait for the API to come up
```

## Verify (before trusting the schedule)

```sh
# 1. Gateway is logged in + the relay is live (app waits on this healthcheck anyway):
docker compose ps                          # gateway should be (healthy)

# 2. A real one-off session end-to-end (preflight -> data -> decide -> book -> email):
docker compose run --rm app python -m src.forward.run --as-of "$(date +%F)" --max-llm-usd 2
#    Expect: "preflight OK: paper account DU..." in the logs, a digest, and a "✅" email.

# 3. The watchdog path (emails only if stale; otherwise logs "heartbeat OK"):
docker compose run --rm app python -m src.forward.run --healthcheck
```

If the test run can't reach the Gateway you'll see the preflight fail fast (before any
LLM spend) and a "❌ FAILED" email — that's the silent-hole protection working.

## Schedule

`deploy/app/crontab` (supercronic), times in `$TZ`:
- **Sun 17:00** — the weekly session, spend-capped by `PARLEY_MAX_LLM_USD`.
- **Daily 09:00** — `--healthcheck`: emails if the last run is missing/errored/older than
  8 days, so a skipped Sunday is caught within a day.

## State & backups

Named volumes:
- **`forward`** → `/app/data/forward` — the **track record** (paper book, digests,
  `last_run.json`). This is the irreplaceable GATE-0 evidence; **back it up** (e.g.
  `docker run --rm -v parley_forward:/v -v "$PWD":/b alpine tar czf /b/forward-backup.tgz -C /v .`).
- **`cache`** → `/app/data/cache` — self-warming (prices from IBKR, EDGAR facts, signals,
  Nasdaq-100 membership). Disposable; rebuilds itself.
- **`gateway_settings`** → `/root/Jts` — Gateway/IBC settings (incl. auto-restart).

**Dividend seed (optional).** The dividend cache (`data/cache/fmp_signals/`, FMP-derived,
gitignored) isn't baked into the image. Without it, dividends simply aren't credited (the
loader returns empty — non-fatal). To enable crediting, copy it into the cache volume once:
```sh
docker compose cp ../data/cache/fmp_signals app:/app/data/cache/fmp_signals   # app running
```

## Operations

```sh
docker compose logs -f app                 # scheduler + run output
docker compose exec app tail -f /app/data/forward/logs/cron.log
docker compose restart gateway             # after changing IBKR/Gateway settings
docker compose build && docker compose up -d   # redeploy after a code/version change
```

## Going live later (deliberate, separate)

The Gateway runs **ReadOnlyApi=yes** and the PaperBook is simulated, so nothing transmits.
Real orders are a separate decision: set `ReadOnlyApi=no` in the IBC config, wire
`broker_rebalance(..., transmit=True)` per [execution.md](execution.md), and only after
the forward record clears GATE 0. The paper-account guard (`assert_paper_ready` /
`_assert_paper`) stays in force regardless.
