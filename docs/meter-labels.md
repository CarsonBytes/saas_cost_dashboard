# Meter app-label registry (spec 2026-09-20)

Single source of truth for the `app` label every Supabase-touching process
flushes into `meter_rollup` (via `supabase_meter`, copied verbatim into each
repo). The dashboard's Supabase tab groups by this label; an unregistered
label renders under `unknown` with an "Unlabeled traffic" card telling you
how to fix it.

## Labels

| Label | Process | Set by |
|---|---|---|
| `study` | study web (Docker) + all native jobs (scrapers, batch, native app) | `SUPABASE_METER_APP=study` in compose; `core/db.py` default |
| `study-demo` | public demo (zero-egress file client) | `SUPABASE_METER_APP=study-demo` in compose |
| `quant-paper` | quant paper dashboard | `SUPABASE_METER_APP=quant-paper` in `docker-compose.yml`; derived from `DASH_FIXED_MODE` otherwise |
| `quant-live` | quant live dashboard | `SUPABASE_METER_APP=quant-live` in `docker-compose.live.yml` |
| `event-radar` | event-radar backend | `SUPABASE_METER_APP=event-radar` in compose / `llm_logging.py` |
| `event-radar-demo` | event-radar demo | `SUPABASE_METER_APP=event-radar-demo` in compose |
| `dashboard` | this repo (Command Deck) | `ledger.py` (`configure(app="dashboard")`) |

## History notes

- `study-native` (retired 2026-09-20, merged into `study`): native jobs used
  to flush under this label. `ledger.py::_APP_ALIASES` maps it to `study` on
  read, so charts are continuous across the rename.
- `quant` (retired 2026-09-20, split into `quant-paper`/`quant-live`):
  paper and live shared one label, making their egress indistinguishable.
  Historical `quant` rows are NOT remappable (mode wasn't recorded) and stay
  as-is.
- `unknown`: catch-all default when no label is configured. Writers attach
  `{host, pid, argv0}` as `detail` (migration 005) so these rows are
  self-identifying. If you see them: set `SUPABASE_METER_APP` on the process
  shown, or add its import path to the `configure()` call in its repo.

## Adding a new process

1. Set `SUPABASE_METER_APP=<label>` in its compose file / unit / scheduler.
2. If it imports `supabase_meter` without that env (tests, one-offs), pass
   `app=` to `configure()` at import.
3. Add the label + color to `_APP_COLORS` in `app.py`.
