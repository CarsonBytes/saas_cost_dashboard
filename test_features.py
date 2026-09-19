"""Fast, network-free unit tests for the Phase 4 feature logic:
cost-spike detection, window totals, KPI delta lines, the monthly-budget
setting (incl. the read-modify-write regression), and noc's 6-hour uptime
slots. UI-level coverage lives in test_render_smoke.py."""
import datetime as dt
import json

import pytest

import alerts
import ledger
import noc


@pytest.fixture
def _meter_isolated(monkeypatch, tmp_path):
    """Hermetic meter state: tmp file, blanked Supabase creds (so a flush can
    never POST to the real project from unit tests), restored afterwards."""
    import supabase_meter
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")
    old = (supabase_meter.FILE, supabase_meter.FLUSH_SEC, supabase_meter.APP,
           supabase_meter._rollup_disabled, supabase_meter.ROLLUP_POST_SEC)
    supabase_meter.configure(path=str(tmp_path / "m.jsonl"))
    supabase_meter.ROLLUP_POST_SEC = 0  # tests that assert per-record POSTs; throttle has its own test
    supabase_meter.FLUSH_SEC = 3600
    supabase_meter.APP = "test"
    supabase_meter._rollup_disabled = False
    supabase_meter.reset()
    yield supabase_meter
    supabase_meter.configure(path=old[0])
    supabase_meter.FLUSH_SEC = old[1]
    supabase_meter.APP = old[2]
    supabase_meter._rollup_disabled = old[3]
    supabase_meter.ROLLUP_POST_SEC = old[4]
    supabase_meter.reset()


# ---- A5: cost spike detection ------------------------------------------------

def test_spike_detected_when_day_exceeds_2x_trailing_mean():
    series = [{"date": f"2026-08-{d:02d}", "calls": 1, "cost_usd": 0.10}
              for d in range(1, 8)]
    series.append({"date": "2026-08-08", "calls": 50, "cost_usd": 0.50})
    spikes = ledger.cost_spike_dates(series)
    assert spikes == {"2026-08-08": 5.0}


def test_no_spike_off_zero_baseline_or_first_day():
    # first day: no trailing window at all; zero-cost history: no positive
    # baseline, so usage starting up must not read as a "spike"
    series = [
        {"date": "2026-08-01", "calls": 1, "cost_usd": 0.00},
        {"date": "2026-08-02", "calls": 1, "cost_usd": 0.00},
        {"date": "2026-08-03", "calls": 40, "cost_usd": 0.30},
    ]
    assert ledger.cost_spike_dates(series) == {}


def test_normal_variation_is_not_a_spike():
    series = [{"date": f"2026-08-{d:02d}", "calls": 1,
               "cost_usd": 0.10 + (d % 3) * 0.01} for d in range(1, 11)]
    assert ledger.cost_spike_dates(series) == {}


# ---- A2: window totals + deltas ------------------------------------------------

def test_window_totals_sums_all_counts():
    rows = [{"cost_usd": 0.25, "prompt_tokens": 100, "completion_tokens": 50},
            {"cost_usd": None, "prompt_tokens": None, "completion_tokens": 10}]
    totals = ledger.window_totals(rows)
    assert totals == {"calls": 2, "cost_usd": 0.25,
                      "prompt_tokens": 100, "completion_tokens": 60}


def test_delta_sub_directions_and_zero_baseline():
    import app
    app.STATE["days"] = 7
    assert app._delta_sub(120.0, 100.0) == ("↑ 20% vs prev 7d", "text-grey-6")
    assert app._delta_sub(80.0, 100.0, lower_is_better=True)[1] == "text-green-700"
    assert app._delta_sub(150.0, 100.0, lower_is_better=True)[1] == "text-red-600"
    assert app._delta_sub(10.0, 0.0) is None       # percentage off zero: meaningless
    assert app._delta_sub(10.0, None) is None      # no previous window


# ---- A3: monthly budget setting -------------------------------------------------

def test_budget_round_trip_and_threshold_coexistence(tmp_path, monkeypatch):
    settings_file = tmp_path / "alert_settings.json"
    monkeypatch.setattr(alerts, "_SETTINGS_FILE", settings_file)
    old_t, old_b = alerts.ALERT_DAILY_COST_USD, alerts.MONTHLY_BUDGET_USD
    try:
        alerts.set_daily_threshold(0.40)
        alerts.set_monthly_budget(25.0)
        assert alerts.MONTHLY_BUDGET_USD == 25.0
        assert alerts.effective_monthly_budget() == 25.0
        # regression: setting the threshold must not erase the budget
        alerts.set_daily_threshold(0.60)
        saved = json.loads(settings_file.read_text())
        assert saved["monthly_budget_usd"] == 25.0
        assert saved["alert_daily_cost_usd"] == 0.60
        # clearing the budget falls back to the implied threshold x 30
        alerts.set_monthly_budget(None)
        assert alerts.MONTHLY_BUDGET_USD is None
        assert alerts.effective_monthly_budget() == 0.60 * 30
    finally:
        alerts.set_daily_threshold(old_t)
        alerts.set_monthly_budget(old_b)


# ---- A4: uptime slots ------------------------------------------------------------

def _state():
    return {"check_slots": {}}


def test_slots_align_to_6h_boundaries_and_cap_at_28():
    now = dt.datetime(2026, 8, 26, 14, 0, tzinfo=dt.timezone.utc)  # inside a slot
    state = _state()
    for i in range(40):   # 40 cycles across >1 slot boundary
        t = now - dt.timedelta(minutes=10 * i)
        noc._record_slot(state, "X", i % 5 != 4, t)   # mostly ok, some fail
    slots = state["check_slots"]["X"]
    assert len(slots) <= noc._SLOT_COUNT
    assert all(s["start"] % noc._SLOT_SEC == 0 for s in slots)


def test_uptime_slots_colors_and_none_fill(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    current_start = int(now.timestamp()) // noc._SLOT_SEC * noc._SLOT_SEC
    fake_state = {"check_slots": {"X": [
        {"start": current_start - 2 * noc._SLOT_SEC, "ok": 10, "fail": 0},   # -> ok
        {"start": current_start - noc._SLOT_SEC, "ok": 5, "fail": 2},        # -> mixed
        {"start": current_start - 5 * noc._SLOT_SEC, "ok": 0, "fail": 9},    # -> fail (gap before it)
    ]}}
    monkeypatch.setattr(noc, "_load_state", lambda: fake_state)
    out = noc.uptime_slots("X")
    assert len(out) == noc._SLOT_COUNT
    assert out[-1] == "none"      # current slot has no checks yet in this fake state
    assert out[-2] == "mixed"
    assert out[-3] == "ok"
    assert out[-6] == "fail"      # oldest recorded slot, with a gap of 'none' before it
    assert out[:noc._SLOT_COUNT - 6].count("none") == noc._SLOT_COUNT - 6  # before the oldest slot


def test_uptime_slots_unknown_agent_is_all_none():
    assert noc.uptime_slots("does-not-exist") == ["none"] * noc._SLOT_COUNT


# ---- B3 support: HKT day-boundary math ---------------------------------------------

def test_hkt_day_start_conversion():
    # HKT midnight 2026-08-26 == 2026-08-25 16:00 UTC
    start = ledger._hkt_day_start_utc("2026-08-26")
    assert start.utcoffset() == dt.timedelta(0)
    assert start.strftime("%Y-%m-%d %H:%M") == "2026-08-25 16:00"


# ---- A1: supervised auto-unlock with escalation --------------------------------

def _meta(locked_min_ago: float, strikes: int = 1, sticky: bool = False):
    now = dt.datetime.now(dt.timezone.utc)
    return {"locked_at": (now - dt.timedelta(minutes=locked_min_ago)).isoformat(),
            "strikes": strikes, "sticky": sticky}


def test_auto_unlock_waits_for_cooldown_then_needs_stable_deps():
    now = dt.datetime.now(dt.timezone.utc)
    # inside the 60-min cooldown: nothing happens
    assert noc._auto_unlock_decision(_meta(30), now, deps_stable=True) is None
    # elapsed + dependencies stable -> one supervised recovery
    assert noc._auto_unlock_decision(_meta(61), now, deps_stable=True) == "unlock"
    # elapsed but a dependency is still down -> stay locked, retry later
    assert noc._auto_unlock_decision(_meta(61), now, deps_stable=False) == "wait-deps"
    # no metadata at all -> never decide from nothing
    assert noc._auto_unlock_decision({}, now, deps_stable=True) is None


def test_second_strike_is_sticky_and_doubles_cooldown():
    now = dt.datetime.now(dt.timezone.utc)
    m = _meta(90, strikes=2, sticky=True)
    # sticky: even elapsed + stable deps NEVER auto-unlocks -- two supervised
    # recoveries failing means restarts aren't healing; only a human proceeds
    assert noc._auto_unlock_decision(m, now, deps_stable=True) is None


def test_lock_strikes_escalate_within_window_reset_outside(monkeypatch):
    monkeypatch.setattr(noc, "_send_lock_alert", lambda name, lock_count=3: True)
    now = dt.datetime.now(dt.timezone.utc)
    state = {}
    noc._lock_agent(state, "X", now)          # fresh problem -> strike 1
    meta = state["lock_meta"]["X"]
    assert meta["strikes"] == 1 and not meta["sticky"]
    # simulate a supervised auto-unlock...
    meta["last_auto_unlock"] = now.timestamp()
    meta.pop("locked_at")
    state.get("locks", {}).pop("X", None)
    # ...and a re-lock 2h later (inside the 6h window) escalates to sticky
    noc._lock_agent(state, "X", now + dt.timedelta(hours=2))
    meta = state["lock_meta"]["X"]
    assert meta["strikes"] == 2 and meta["sticky"] is True
    # ...but a lock >6h after the last auto-unlock starts fresh at strike 1
    meta["last_auto_unlock"] = (now - dt.timedelta(hours=7)).timestamp()
    noc._lock_agent(state, "X", now)
    meta = state["lock_meta"]["X"]
    assert meta["strikes"] == 1 and not meta["sticky"]


# ---- A3: maintenance window ------------------------------------------------------

def test_maintenance_window_lifecycle(tmp_path, monkeypatch):
    state_file = tmp_path / "noc_state.json"
    monkeypatch.setattr(noc, "_STATE_FILE", state_file)
    try:
        assert noc.maintenance_active() is None
        noc.set_maintenance(30)
        assert noc.maintenance_active() is not None
        assert noc.maintenance_active("Quant Trading (Paper)") is not None  # global covers all
        assert noc.maintenance_remaining_min() >= 29
        noc.clear_maintenance()
        assert noc.maintenance_active() is None
        # scoped window: named agents covered, others not
        noc.set_maintenance(15, scope="Quant Trading (Paper)")
        assert noc.maintenance_active("Quant Trading (Paper)") is not None
        assert noc.maintenance_active("Study Platform") is None
        noc.clear_maintenance()
    finally:
        if state_file.exists():
            state_file.unlink()


# ---- A2: Telegram command handlers -------------------------------------------------

def test_command_handlers(monkeypatch):
    import commands
    unlocked, muted, cleared = [], [], []
    monkeypatch.setattr(noc, "clear_lock", lambda n: unlocked.append(n))
    monkeypatch.setattr(noc, "set_maintenance", lambda m, scope="all": muted.append((m, scope)))
    monkeypatch.setattr(noc, "clear_maintenance", lambda: cleared.append(True))

    # /unlock matches by substring, demands specificity on ambiguity
    reply = commands.handle_text("/unlock paper")
    assert reply and "Quant Trading (Paper)" in reply   # unlocked, or not locked
    assert unlocked == ["Quant Trading (Paper)"]
    ambiguous = commands.handle_text("/unlock quant")
    assert ambiguous and "specific" in ambiguous.lower()   # Paper AND Live match
    # unknown agent -> helpful, no crash
    assert "0 match" in commands.handle_text("/unlock zzz")

    # /mute global vs scoped
    assert "maintenance" in commands.handle_text("/mute 30").lower()
    assert muted[-1] == (30, "all")
    commands.handle_text("/mute 15 study")
    assert muted[-1][0] == 15 and "Study Platform" in muted[-1][1]
    assert "usage" in commands.handle_text("/mute").lower()

    # /status lists monitored agents and never raises without network extras
    status = commands.handle_text("/status")
    assert "Quant Trading (Paper)" in status and "$" in status

    # non-commands stay silent
    assert commands.handle_text("hello?") is None
    assert commands.handle_text("/definitely-not-a-command x") is None


# ---- Supabase self-meter (2026-09-13) -------------------------------------------------

def test_meter_counts_and_snapshots(_meter_isolated):
    supabase_meter = _meter_isolated
    supabase_meter.record("GET", "llm_calls")
    supabase_meter.record("GET", "llm_calls")
    supabase_meter.record("POST", "governance_audit_log")
    assert supabase_meter.snapshot() == {"GET llm_calls": 2, "POST governance_audit_log": 1}
    supabase_meter.reset()
    assert supabase_meter.snapshot() == {}


def test_meter_flushes_jsonl_and_read_totals(_meter_isolated):
    supabase_meter = _meter_isolated
    supabase_meter.FLUSH_SEC = 0  # flush on every record in this test
    supabase_meter.record("GET", "llm_calls")
    supabase_meter.record("GET", "llm_daily_summary")
    # flushed lines cleared the in-memory counts
    assert supabase_meter.snapshot() == {}
    totals = supabase_meter.read_totals(supabase_meter.FILE)
    assert totals == {"GET llm_calls": 1, "GET llm_daily_summary": 1}


def test_meter_response_hook_parses_postgrest_urls(_meter_isolated):
    supabase_meter = _meter_isolated
    from types import SimpleNamespace

    def _resp(method, path):
        return SimpleNamespace(request=SimpleNamespace(
            method=method, url=SimpleNamespace(path=path)))

    supabase_meter.response_hook(_resp("GET", "/rest/v1/llm_calls"))
    supabase_meter.response_hook(_resp("GET", "/rest/v1/llm_calls"))
    supabase_meter.response_hook(_resp("POST", "/rest/v1/governance_audit_log"))
    supabase_meter.response_hook(_resp("GET", "/auth/v1/user"))  # not PostgREST: ignored
    supabase_meter.response_hook(_resp("GET", "/rest/v1/"))      # no table: ignored
    supabase_meter.response_hook(object())                        # garbage: never raises
    assert supabase_meter.snapshot() == {
        "GET llm_calls": 2, "POST governance_audit_log": 1}


def test_meter_tracks_bytes_and_filters_by_app(_meter_isolated):
    import time
    supabase_meter = _meter_isolated
    supabase_meter.APP = "study"
    supabase_meter.record("GET", "questions", 194000)
    supabase_meter.record("GET", "questions", 6000)
    assert supabase_meter.snapshot() == {"GET questions": 2}
    assert supabase_meter.snapshot_bytes() == {"GET questions": 200000}
    supabase_meter.reset()
    # now flush every record (SEC=0): one JSONL line per record
    supabase_meter.FLUSH_SEC = 0
    supabase_meter.record("GET", "questions", 194000)
    supabase_meter.record("GET", "questions", 6000)
    supabase_meter.APP = "study-demo"
    supabase_meter.record("GET", "questions", 1000)
    target = supabase_meter.FILE
    lines = [json.loads(l) for l in open(target, encoding="utf-8")]
    assert len(lines) == 3
    assert lines[0]["bytes"] == {"GET questions": 194000}
    now = time.time()
    assert supabase_meter.read_bytes(target, since_ts=now - 3600) == \
        {"GET questions": 201000}
    assert supabase_meter.read_bytes(target, app="study-demo") == \
        {"GET questions": 1000}
    assert supabase_meter.read_totals(target, app="study") == \
        {"GET questions": 2}


def test_meter_response_bytes_prefers_content_length(_meter_isolated):
    supabase_meter = _meter_isolated
    from types import SimpleNamespace

    with_header = SimpleNamespace(headers={"content-length": "1234"}, content=b"x" * 5)
    assert supabase_meter.response_bytes(with_header) == 1234
    chunked = SimpleNamespace(headers={}, content=b"y" * 77)
    assert supabase_meter.response_bytes(chunked) == 77
    assert supabase_meter.response_bytes(object()) == 0


def test_meter_never_raises_on_bad_file(_meter_isolated, tmp_path):
    supabase_meter = _meter_isolated
    supabase_meter.configure(path=str(tmp_path / "no-such-dir" / "m.jsonl"))
    supabase_meter.FLUSH_SEC = 0
    supabase_meter.record("GET", "llm_calls")  # must not raise
    assert supabase_meter.read_totals(str(tmp_path / "missing.jsonl")) == {}


def test_reported_usage_parses_shapes_and_caches(monkeypatch, tmp_path):
    import supabase_usage
    monkeypatch.setenv("SUPABASE_URL", "https://abcdef.supabase.co")
    monkeypatch.setenv("SUPABASE_MANAGEMENT_TOKEN", "sbp_test")
    monkeypatch.setattr(supabase_usage, "_CACHE_FILE", tmp_path / "u.json")
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def _fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        assert "abcdef" in url and url.endswith("/analytics/endpoints/usage.api-counts")
        assert params == {"interval": "1day"}
        assert headers == {"Authorization": "Bearer sbp_test"}
        # Real shape verified live 2026-09-13: hourly buckets, only
        # total_rest_requests matters (matches what the self-meters count).
        return _Resp({"result": [
            {"timestamp": "2026-09-13T13:00:00", "total_rest_requests": 40000,
             "total_auth_requests": 0, "total_realtime_requests": 0, "total_storage_requests": 0},
            {"timestamp": "2026-09-12T13:00:00", "total_rest_requests": 3000,
             "total_auth_requests": 0, "total_realtime_requests": 0, "total_storage_requests": 0},
        ]})

    monkeypatch.setattr(supabase_usage.httpx, "get", _fake_get)
    first = supabase_usage.fetch_reported_usage()
    assert first is not None
    total, note = supabase_usage.reported_requests(first)
    assert total == 43000
    assert "total_rest_requests" in note
    second = supabase_usage.fetch_reported_usage()  # hourly cache: no 2nd call
    assert len(calls) == 1 and second == first
    # dict-form actions + unconfigured token
    total2, _ = supabase_usage.reported_requests(
        {"payload": {"data": {"read": 10, "write": 5}}})
    assert total2 == 15
    monkeypatch.setenv("SUPABASE_MANAGEMENT_TOKEN", "")
    monkeypatch.setattr(supabase_usage, "_CACHE_FILE", tmp_path / "empty.json")
    assert supabase_usage.fetch_reported_usage() is None  # unconfigured, cold cache
    assert supabase_usage.reported_requests(None)[0] is None


def _isolated_rollup_store(monkeypatch, tmp_path):
    import ledger
    monkeypatch.setattr(ledger, "_rollup_store", None)
    monkeypatch.setattr(ledger, "_ROLLUP_STORE_FILE", tmp_path / "hourly.json")
    ledger._ROLLUP_CACHE.update({"ts": 0.0, "rows": [], "key": ""})


def test_ledger_rollup_returns_empty_without_table(monkeypatch, tmp_path):
    import ledger
    _isolated_rollup_store(monkeypatch, tmp_path)

    class _Resp:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("must not be called on 404")

        def json(self):
            raise AssertionError("must not be called on 404")

    monkeypatch.setattr(ledger.httpx, "get", lambda *a, **k: _Resp())
    ledger._ROLLUP_CACHE.update({"ts": 0.0, "rows": []})
    assert ledger.fetch_meter_rollup() == []
    # cached (even empty): no second HTTP call within the window
    ledger.fetch_meter_rollup()


def test_meter_rollup_posts_and_disables_on_404(_meter_isolated):
    import urllib.error
    import urllib.request
    supabase_meter = _meter_isolated
    supabase_meter.FLUSH_SEC = 0
    posted = []

    class _Resp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        posted.append((req.full_url, json.loads(req.data)))
        return _Resp()

    import os
    old_url = os.environ.get("SUPABASE_URL", "")
    old_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    os.environ["SUPABASE_URL"] = "https://x.supabase.co"
    os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "key"
    orig = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        supabase_meter.APP = "dashboard"
        supabase_meter.record("GET", "llm_calls", 500)
        assert len(posted) == 1
        url, rows = posted[0]
        assert url.endswith("/rest/v1/meter_rollup")
        assert len(rows) == 1 and rows[0]["endpoint"] == "GET llm_calls"
        assert (rows[0]["requests"], rows[0]["bytes"]) == (1, 500)
        assert rows[0]["app"] == "dashboard" and "ts" in rows[0]

        def _raise_404(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        urllib.request.urlopen = _raise_404
        supabase_meter.record("GET", "llm_calls", 1)
        assert supabase_meter._rollup_disabled is True
        supabase_meter.record("GET", "llm_calls", 1)
        assert len(posted) == 1  # disabled: no further attempts
    finally:
        urllib.request.urlopen = orig
        if old_url:
            os.environ["SUPABASE_URL"] = old_url
        else:
            os.environ.pop("SUPABASE_URL", None)
        if old_key:
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = old_key
        else:
            os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)


def test_meter_rollup_retries_pending_batch_on_transient_failure(_meter_isolated):
    """FIXED 2026-09-17: a rollup POST failing for any reason OTHER than 404
    (timeout, 5xx, connection reset) must not silently drop that window's
    data -- found live via a real ~46% undercount in meter_rollup vs the
    local file for the same app/window. The failed batch must still be
    included (merged) in the NEXT attempt, and only a confirmed 2xx clears
    it."""
    import urllib.request
    supabase_meter = _meter_isolated
    supabase_meter.FLUSH_SEC = 0
    posted = []

    class _Resp:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fail_then_succeed(req, timeout=None):
        rows = json.loads(req.data)
        posted.append(rows)
        if len(posted) == 1:
            raise TimeoutError("simulated transient network failure")
        return _Resp(201)

    import os
    old_url = os.environ.get("SUPABASE_URL", "")
    old_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    os.environ["SUPABASE_URL"] = "https://x.supabase.co"
    os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "key"
    orig = urllib.request.urlopen
    urllib.request.urlopen = _fail_then_succeed
    try:
        supabase_meter.APP = "study"
        supabase_meter.record("GET", "questions", 100)   # attempt 1: fails (TimeoutError)
        assert supabase_meter._rollup_disabled is False  # non-404 must NOT disable
        supabase_meter.record("GET", "questions", 200)   # attempt 2: succeeds
        assert len(posted) == 2
        # The second (successful) attempt's payload must include BOTH the
        # first (failed, retried) batch and the second -- merged, not lost.
        final_rows = {r["endpoint"]: r for r in posted[1]}
        assert final_rows["GET questions"]["requests"] == 2
        assert final_rows["GET questions"]["bytes"] == 300
        # Pending buffer is now empty -- a third record() starts a fresh batch.
        supabase_meter.record("GET", "questions", 50)
        assert len(posted) == 3
        assert posted[2] == [{"ts": posted[2][0]["ts"], "app": "study",
                              "endpoint": "GET questions", "requests": 1, "bytes": 50}]
    finally:
        urllib.request.urlopen = orig
        if old_url:
            os.environ["SUPABASE_URL"] = old_url
        else:
            os.environ.pop("SUPABASE_URL", None)
        if old_key:
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = old_key
        else:
            os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)


def test_meter_rollup_pages_past_1000_rows_and_settles_history(monkeypatch, tmp_path):
    """PostgREST caps responses at 1,000 rows: the fetch must page, fold settled
    hours into the persistent store, and only refetch the unsettled tail."""
    import datetime as _dt
    import ledger
    _isolated_rollup_store(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger, "SUPABASE_URL", "http://x")
    monkeypatch.setattr(ledger, "SUPABASE_SERVICE_ROLE_KEY", "k")
    now = _dt.datetime.now(_dt.timezone.utc)
    settled_h = ledger._hour_floor(now - _dt.timedelta(seconds=ledger._ROLLUP_SETTLE_SEC))
    old = settled_h - _dt.timedelta(hours=5)
    data = [{"ts": (old + _dt.timedelta(seconds=i)).isoformat(), "app": "a", "endpoint": "GET t",
             "requests": 1, "bytes": 10} for i in range(2500)]
    data.append({"ts": (settled_h + _dt.timedelta(seconds=1)).isoformat(), "app": "a",
                 "endpoint": "GET t", "requests": 7, "bytes": 70})
    calls = []

    class _Resp:
        status_code = 200

        def __init__(self, rows):
            self._rows = rows
            self.headers = {}
            self.content = b"[]"

        def raise_for_status(self):
            pass

        def json(self):
            return self._rows

    def _fake_get(url, params=None, **k):
        p = dict(params)
        lo = _dt.datetime.fromisoformat(p["ts"].removeprefix("gte."))
        off = int(p["offset"])
        calls.append(off)
        rows = [r for r in data if _dt.datetime.fromisoformat(r["ts"]) >= lo]
        return _Resp(rows[off:off + 1000])

    monkeypatch.setattr(ledger.httpx, "get", _fake_get)
    since = (old - _dt.timedelta(hours=1)).isoformat()
    rows = ledger.fetch_meter_rollup(since_iso=since)
    assert sum(r["requests"] for r in rows) == 2500 + 7  # nothing truncated at 1000
    assert calls == [0, 1000, 2000]
    assert (tmp_path / "hourly.json").exists()
    # settled history is now stored: an immediate refresh over the same window
    # is served from the 5-min tail cache with no further HTTP calls
    calls.clear()
    again = ledger.fetch_meter_rollup(since_iso=since)
    assert sum(r["requests"] for r in again) == 2507 and calls == []
    # until_iso bounds the window (a past custom range must not include "now")
    bounded = ledger.fetch_meter_rollup(
        since_iso=since, until_iso=(settled_h - _dt.timedelta(hours=1)).isoformat())
    assert sum(r["requests"] for r in bounded) == 2500 and calls == []


def test_reported_requests_filters_buckets_to_window():
    import supabase_usage as su
    buckets = [{"timestamp": f"2026-09-1{d}T16:00:00", "total_rest_requests": 100 * d} for d in range(1, 5)]
    cached = {"interval": "7day", "payload": {"result": buckets}}
    utc = dt.timezone.utc
    assert su.reported_requests(cached)[0] == 1000
    assert su.reported_requests(
        cached, since=dt.datetime(2026, 9, 12, 16, tzinfo=utc),
        until=dt.datetime(2026, 9, 14, 16, tzinfo=utc))[0] == 500  # buckets 2 and 3
    assert su.reported_requests(cached, since=dt.datetime(2027, 1, 1, tzinfo=utc))[0] is None
    assert su.reported_first_ts(cached) == dt.datetime(2026, 9, 11, 16, tzinfo=utc)


def test_llm_rows_window_freezes_closed_days(monkeypatch, tmp_path):
    """Days older than yesterday are fetched once and never again; only
    yesterday + today hit Supabase on later loads."""
    import datetime as _dt
    monkeypatch.setattr(ledger, "_llm_days", None)
    monkeypatch.setattr(ledger, "_LLM_DAYS_FILE", tmp_path / "days.json")
    today = ledger._hkt_today_start_utc()
    calls = []

    def _fake_raw(start, end=None, select="", fill=True):
        calls.append((start, end))
        rows, t = [], start
        stop = end or today + _dt.timedelta(days=1)
        while t < stop:
            rows.append({"created_at": (t + _dt.timedelta(hours=3)).isoformat(), "cost_usd": 1.0})
            t += _dt.timedelta(days=1)
        return rows

    monkeypatch.setattr(ledger, "_fetch_rows_window_raw", _fake_raw)
    start = today - _dt.timedelta(days=9)  # 10 days incl. today
    first = ledger._fetch_rows_window(start, today + _dt.timedelta(days=1))
    assert len(first) == 10
    assert first == sorted(first, key=lambda r: r["created_at"], reverse=True)
    assert len(calls) == 2  # one run of 8 frozen days + the live yesterday/today segment
    calls.clear()
    again = ledger._fetch_rows_window(start, today + _dt.timedelta(days=1))
    assert len(again) == 10
    live_from = today if _dt.datetime.now(ledger._HKT).hour >= 1 else today - _dt.timedelta(days=1)
    assert len(calls) == 1 and calls[0][0] == live_from  # only the live segment
    assert (tmp_path / "days.json").exists()


def test_governance_tables_state_distinguishes_transient_from_missing(monkeypatch):
    from governance import engine
    monkeypatch.setattr(engine, "_TABLES_CONFIRMED", False)
    states = iter(["ok", "error"])
    monkeypatch.setattr(engine, "_table_state", lambda t: next(states))
    assert engine.tables_state() == "unknown"   # timeout != "tables not created"
    states2 = iter(["missing", "ok"])
    monkeypatch.setattr(engine, "_table_state", lambda t: next(states2))
    assert engine.tables_state() == "missing"
    monkeypatch.setattr(engine, "_table_state", lambda t: "ok")
    assert engine.tables_state() == "ready"
    monkeypatch.setattr(engine, "_table_state", lambda t: (_ for _ in ()).throw(AssertionError("latched: no re-probe")))
    assert engine.tables_state() == "ready"
    monkeypatch.setattr(engine, "_TABLES_CONFIRMED", False)


def test_meter_rollup_post_is_throttled_but_loses_nothing(_meter_isolated, monkeypatch):
    import urllib.request
    m = _meter_isolated
    m.FLUSH_SEC = 0
    m.ROLLUP_POST_SEC = 300
    monkeypatch.setenv("SUPABASE_URL", "http://x")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    posted = []

    class _Ok:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake(req, timeout=None):
        posted.append(json.loads(req.data))
        return _Ok()

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    m.record("GET", "t", 100)          # first flush posts immediately
    m.record("GET", "t", 200)          # within 300s: held in the pending buffer
    m.record("GET", "t", 300)
    assert len(posted) == 1 and posted[0][0]["requests"] == 1
    m._rollup_last_post -= 301         # 5 minutes later
    m.record("GET", "t", 400)
    assert len(posted) == 2
    assert posted[1][0]["requests"] == 3 and posted[1][0]["bytes"] == 900  # the two held batches + this one


def test_egress_series_buckets_by_app_and_fills_gaps():
    import app as deck
    utc = dt.timezone.utc
    start = dt.datetime(2026, 9, 16, 16, tzinfo=utc)   # HKT midnight 09-17
    end = start + dt.timedelta(days=1)
    rows = [
        {"ts": "2026-09-16T16:30:00+00:00", "app": "a", "requests": 5, "bytes": 2_000_000},
        {"ts": "2026-09-16T16:45:00+00:00", "app": "a", "requests": 1, "bytes": 1_000_000},
        {"ts": "2026-09-16T19:00:00+00:00", "app": "b", "requests": 7, "bytes": 500_000},
    ]
    out = deck._egress_series(rows, start, end, now=end)
    assert out["labels"][0] == "09-17 00:00" and len(out["labels"]) == 24  # hourly, empty hours kept
    assert out["apps"]["a"]["req"][0] == 6 and out["apps"]["a"]["mb"][0] == 3.0
    assert out["apps"]["b"]["req"][3] == 7 and out["apps"]["b"]["req"][1] == 0
    week = deck._egress_series(rows, start, start + dt.timedelta(days=7), now=start + dt.timedelta(days=7))
    assert len(week["labels"]) == 7 and week["labels"][0] == "09-17"      # daily beyond 2 days
    assert week["apps"]["a"]["req"][0] == 6
