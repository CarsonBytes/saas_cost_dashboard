"""Render-path + interaction tests for the Phase 4 changes -- real websocket
clients via NiceGUI's user simulation (plain HTTP GETs never execute the page
function). Covers: page render/tabs, KPI deltas, project filter chip,
incident/audit filters, custom date range apply/clear, dark-mode persistence,
threshold/budget Enter-to-save, staleness indicator, uptime strips, CSV
exports. Pure-function coverage lives in test_features.py."""
import datetime as dt

import pytest
from nicegui.testing import User

import app  # noqa: F401 -- registers the page; ui.run stays guarded

# The user fixture executes deck_test_main.py via runpy as __main__; it
# reloads the same 'app' module object, so tests share STATE with the UI.
MAIN = pytest.mark.nicegui_main_file("deck_test_main.py")


def _one(user: User, marker: str):
    from nicegui import ElementFilter
    with user:
        found = list(ElementFilter(marker=marker))
    assert found, f"no element marked {marker!r}"
    return found[0]


def _set(user: User, element, value) -> None:
    """Set an input's value inside the client context (fires on_change)."""
    with user.client:
        element.set_value(value)


def _trigger(user: User, element, event: str) -> None:
    from nicegui.testing.user_interaction import UserInteraction
    UserInteraction(user, {element}, None).trigger(event)


# ---- page shell -------------------------------------------------------------

@MAIN
async def test_page_renders_with_all_tabs(user: User):
    await user.open("/")
    await user.should_see("Command Deck")
    await user.should_see("Total cost")
    await user.should_see("Overview")
    await user.should_see("Cost & Usage")
    await user.should_see("Reliability & Incidents")
    await user.should_see("Governance")


@MAIN
async def test_kpi_deltas_present_when_prev_window_has_rows(user: User):
    await user.open("/")
    await user.should_see("vs prev")


# ---- A1: per-project drill-down ----------------------------------------------

@MAIN
async def test_project_filter_chip_appears(user: User):
    # Set the filter + refetch BEFORE opening the page (the real UI path goes
    # through the card button inside a live handler; here no client exists yet,
    # so refresh_all() must not be called).
    import app as deck
    deck.STATE["project"] = "quant"
    deck.fetch_stats()
    assert deck.STATE["data"] and deck.STATE["data"]["range_days"] >= 1
    await user.open("/")
    await user.should_see("Costs filtered to: quant")
    deck.STATE["project"] = None
    deck.fetch_stats()


# ---- B4: incident / audit filters --------------------------------------------

@MAIN
async def test_incident_log_filter_narrows_rows(user: User):
    await user.open("/")
    await user.should_see("Incident log")
    box = _one(user, "incident-filter")
    _set(user, box, "zzz-no-match-zzz")   # fires on_change -> refresh -> filtered render
    await user.should_see("(no matching incidents)")
    _set(user, box, "")                   # clearing restores the unfiltered log
    await user.should_not_see("(no matching incidents)")


@MAIN
async def test_audit_trail_filter_narrows_rows(user: User):
    await user.open("/")
    await user.should_see("Audit trail")
    box = _one(user, "audit-filter")
    _set(user, box, "zzz-no-match-zzz")
    await user.should_see("(no matching audit entries)")
    _set(user, box, "")
    await user.should_not_see("(no matching audit entries)")


# ---- B3: custom date range ----------------------------------------------------

@MAIN
async def test_custom_range_apply_and_clear(user: User):
    import app as deck
    await user.open("/")
    start, end = dt.date.today() - dt.timedelta(days=6), dt.date.today()
    _set(user, _one(user, "range-start"), start.isoformat())
    _set(user, _one(user, "range-end"), end.isoformat())
    _trigger(user, _one(user, "apply-custom"), "click")
    for _ in range(30):                    # fetch_stats runs in the handler
        if deck.STATE.get("custom") == (start.isoformat(), end.isoformat()) \
                and deck.STATE["data"]:
            break
        await __import__("asyncio").sleep(0.5)
    assert deck.STATE["custom"] == (start.isoformat(), end.isoformat())
    assert deck.STATE["days"] == 7        # inclusive 7-calendar-day window
    assert deck.STATE["error"] is None
    _trigger(user, _one(user, "clear-custom"), "click")
    for _ in range(20):
        if deck.STATE.get("custom") is None:
            break
        await __import__("asyncio").sleep(0.5)
    assert deck.STATE["custom"] is None


# ---- B1 + B5: dark mode persistence, threshold/budget Enter-to-save ----------

@MAIN
async def test_dark_mode_toggle_persists_across_reload(user: User):
    await user.open("/")
    toggle = _one(user, "dark-toggle")
    _trigger(user, toggle, "click")       # handler writes app.storage.user
    assert toggle._props["icon"] == "light_mode"
    await user.open("/")                  # a RELOAD must come back dark
    fresh = _one(user, "dark-toggle")
    assert fresh._props["icon"] == "light_mode", "dark mode did not persist"


@MAIN
async def test_threshold_enter_saves(monkeypatch, tmp_path, user: User):
    import alerts
    settings_copy = tmp_path / "alert_settings.json"
    if alerts._SETTINGS_FILE.exists():
        settings_copy.write_text(alerts._SETTINGS_FILE.read_text())
    monkeypatch.setattr(alerts, "_SETTINGS_FILE", settings_copy)
    old_threshold, old_budget = alerts.ALERT_DAILY_COST_USD, alerts.MONTHLY_BUDGET_USD
    try:
        await user.open("/")
        thr = _one(user, "threshold-input")
        budget = _one(user, "budget-input")
        _set(user, thr, 0.75)
        _set(user, budget, 42.0)
        _trigger(user, thr, "keydown.enter")          # Enter-to-save on threshold
        assert alerts.ALERT_DAILY_COST_USD == 0.75
        _trigger(user, budget, "keydown.enter")       # ...and on budget
        assert alerts.MONTHLY_BUDGET_USD == 42.0
        # both values share one file -- neither may clobber the other
        saved = __import__("json").loads(settings_copy.read_text())
        assert saved["alert_daily_cost_usd"] == 0.75
        assert saved["monthly_budget_usd"] == 42.0
    finally:
        alerts.set_daily_threshold(old_threshold)
        alerts.set_monthly_budget(old_budget)


# ---- B2: stale-data indicator ---------------------------------------------------

@MAIN
async def test_stale_indicator_when_last_fetch_is_old(user: User):
    import app as deck
    await user.open("/")
    deck.STATE["last_fetch"] = (
        dt.datetime.now(dt.timezone.utc)
        - dt.timedelta(seconds=3 * deck._ALERT_CHECK_INTERVAL_SEC))
    with user.client:
        deck.last_refreshed_label.refresh()
    await user.should_see("stale")


# ---- A4: uptime strips ------------------------------------------------------------

@MAIN
async def test_uptime_strip_renders_28_slots(user: User):
    from nicegui import ElementFilter, ui
    await user.open("/")
    slots = 0
    for _ in range(30):                    # first health cycle fills the cache
        with user:
            slots = sum(1 for e in ElementFilter(kind=ui.element)
                        if "rounded-sm" in e._classes and "h-3" in e._classes)
        if slots >= 28:
            break
        await __import__("asyncio").sleep(0.5)
    assert slots >= 28, "expected at least one agent card with a 28-slot strip"


# ---- A6: CSV exports build without error ------------------------------------------

@MAIN
async def test_csv_exports_execute(user: User):
    import app as deck
    await user.open("/")
    data = deck.STATE["data"]
    with user.client:
        deck._download_call_types_csv(data)
        deck._download_model_usage_csv(data)
        deck._download_latency_csv(data)
