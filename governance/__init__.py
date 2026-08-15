"""Governance module: accountability-driven risk ledger + compliance radar.

Submodules:
  engine.py -- the Compliance Radar: deterministic rule matching, deadline
               enforcement (PENDING -> OVERDUE), audit trail, Telegram alerts.
               The risk-ledger scanner itself lives in ledger.py.

The package re-exports engine's public functions so callers can use
`governance.check_pending_rules()` etc. directly.
"""
from governance.engine import (  # noqa: F401
    check_pending_rules,
    fetch_compliance_snapshot,
    fetch_rules,
    get_audit_log,
    mark_complied,
    tables_ready,
    _parse_ts,
)
