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

- A small always-on Linux host (amd64) with Docker + Docker Compose v2 (a $5–10/mo VPS is
  plenty; the job runs minutes/week). Region/latency is irrelevant at weekly cadence.
- IBKR **paper** account credentials + a US **market-data subscription** (~$10/mo) on it,
  or price bars come back empty.
- An SMTP sender for alerts (Gmail/Fastmail app password is simplest; Proton needs Bridge).
- A **Doppler** account (free tier) — secrets live there, not in a file. See below.

## Secrets — Doppler (runtime injection, nothing real on disk)

Every secret lives in Doppler and is fetched at container start by `doppler run` (wrapped
into both entrypoints); only a scoped, read-only **service token** sits on the VPS. Honest
caveats: that token can itself fetch everything (the irreducible bootstrap secret — the win
is central rotation/audit/instant revocation), and it adds a runtime dependency on Doppler's
API (mitigated by the CLI's auto encrypted-fallback cache). Infisical works the same way if
you swap the CLI install + `infisical run`.

1. In Doppler: create project **parley**, config **prd**, and add these secrets:
   `TWS_USERID`, `TWS_PASSWORD`, `ANTHROPIC_API_KEY`, `EDGAR_USER_AGENT`,
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `ALERT_EMAIL_TO`,
   `PARLEY_MAX_LLM_USD`. (Non-secret wiring — `IBKR_HOST/PORT`, `TRADING_MODE`, `TZ` — stays
   in compose, not Doppler.)
2. Create a **read-only service token** scoped to `parley/prd`.
3. On the VPS, put just that token in `deploy/.env` and lock it down:
   ```sh
   cp .env.example .env
   echo 'DOPPLER_TOKEN=dp.st.prd.xxxxx' > .env   # the real token
   chmod 600 .env
   ```

## Setup

```sh
# on the host (code already present)
cd parley/deploy
docker compose build                       # first build pulls Gateway + IBC + Doppler + deps
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

## Execution mode

This deployment **executes on the IBKR paper account** (`--execute ibkr --transmit`):
each run reads the real account, sizes decisions via the risk layer, and places whole-share
market orders that fill on the paper account (visible in the IBKR portal). `ReadOnlyApi=no`
in the IBC config permits it; the paper-account guard (`assert_paper_ready` / `_assert_paper`)
still refuses any non-`DU` account. The simulated `--execute sim` path remains for offline
runs. Going to a *live* account is a further deliberate step (real money + the same flags).

## Schedule

`deploy/app/crontab` (supercronic), **TZ = America/New_York** so the schedule is
US-market-relative across DST:
- **Mon 10:00 ET** — the weekly session, executed on the paper account (30 min after the
  open so market orders fill on real liquidity), spend-capped by `PARLEY_MAX_LLM_USD`.
  Inline LLM (`--no-batch`) for predictable timing. (US Monday holidays are an edge — the
  market's closed and orders won't fill; re-run by hand that week if it matters.)
- **Daily 08:00 ET** — `--healthcheck`: Telegram alert if the last run is missing/errored/
  older than 8 days, so a skipped Monday is caught within a day.

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

## Going to a live (real-money) account (deliberate, separate)

Today this trades the **paper** account. A real account is a further deliberate step — and
only after the forward record clears GATE 0. It would mean pointing `TWS_USERID/PASSWORD`
at the live login and relaxing the `DU`-prefix guard, both of which should be done
consciously, with the risk layer and spend caps verified. Until then the guard hard-refuses
any non-paper account.
