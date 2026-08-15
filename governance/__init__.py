"""Governance module: accountability-driven risk ledger + compliance radar.

Submodules:
  engine.py -- the Compliance Radar: deterministic rule matching, deadline
               enforcement (PENDING -> OVERDUE), audit trail, Telegram alerts.
               The risk-ledger scanner itself lives in ledger.py.

The package re-exports engine's public functions so callers can use
`governance.check_pending_rules()` etc. directly.
"""
from governance.engine import (  # noqa: F401
    auto_quarantine_targets,
    build_report,
    cached_audit,
    cached_complied,
    cached_rules,
    cached_tables_ready,
    check_pending_rules,
    compliance_health,
    fetch_compliance_snapshot,
    fetch_rules,
    get_audit_log,
    ingest_regulatory_updates,
    mark_complied,
    refresh_cache,
    tables_ready,
    _parse_ts,
)
