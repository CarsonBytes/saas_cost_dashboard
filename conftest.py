pytest_plugins = ["nicegui.testing.user_plugin"]

import pytest  # noqa: E402

import app  # noqa: F401,E402 -- registers the page; ui.run stays guarded
import governance  # noqa: E402
import noc  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _seed_caches_once():
    """Run the real health/compliance cycles exactly ONCE, before any test's
    timing-sensitive render assertions start, so noc._STATUS_CACHE and
    governance's cache aren't empty for the handful of tests that read real
    data out of them (uptime strip, recent-incidents expander, audit trail).
    Outside any per-test budget, so real network latency here is harmless."""
    noc.refresh_health()
    governance.check_pending_rules()


@pytest.fixture(autouse=True)
def _no_background_network(monkeypatch):
    """FIXED 2026-08-29: app.py's background loops (_services_check_loop,
    _compliance_loop) do real network I/O on every iteration -- noc.
    refresh_health probes every monitored service's live URLs (now 5,
    several behind Cloudflare Access redirect chains), governance.
    check_pending_rules hits Supabase directly. Two tests already neutered
    refresh_health locally because they needed deterministic control over
    _STATUS_CACHE; the same real-network calls were also intermittently
    starving the shared asyncio default thread pool that fetch_stats's own
    to_thread call draws from, tipping unrelated tests' page renders past
    the 3s test-readiness budget (confirmed live via timing instrumentation:
    adding a 5th monitored service, one behind an Access redirect chain,
    made refresh_health's per-cycle duration long enough to reproduce this
    on tests that touch no NOC/governance state at all). _seed_caches_once
    above already populated both caches for real, once; this just stops the
    background loops' ONGOING repeated real calls during the timed test run
    -- their output isn't what's being tested past that first seed."""
    monkeypatch.setattr(noc, "refresh_health", lambda: None)
    monkeypatch.setattr(governance, "check_pending_rules", lambda: {"ok": True})

