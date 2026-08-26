"""Fast, network-free unit tests for the Phase 4 feature logic:
cost-spike detection, window totals, KPI delta lines, the monthly-budget
setting (incl. the read-modify-write regression), and noc's 6-hour uptime
slots. UI-level coverage lives in test_render_smoke.py."""
import datetime as dt
import json

import alerts
import ledger
import noc


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
