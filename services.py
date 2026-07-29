"""Static registry + live health-check for the other carsonng.com properties,
shown as a "My Services" strip above the cost dashboard. Turns this app into
a personal ops hub, not just a cost viewer.

Health check is a plain reachability probe (did we get *any* HTTP response,
not a specific status code) -- quant/quant-live sit behind Cloudflare Access,
so a healthy instance still answers with a redirect to the Access login page,
not a 200. A connection error/timeout is the only thing that means "down".
"""
from __future__ import annotations

import time

import httpx

SERVICES = [
    {
        "name": "Quant Trading",
        "desc": "Weekly-TSMOM dashboard, live paper + live 17-ETF trading",
        "icon": "show_chart",
        "links": [("Paper", "https://quant.carsonng.com"), ("Live", "https://quant-live.carsonng.com")],
    },
    {
        "name": "Event Radar",
        "desc": "AI event-discovery portfolio app, real HK events",
        "icon": "event",
        "links": [("Open", "https://events.carsonng.com")],
    },
    {
        "name": "Study Platform",
        "desc": "Supabase + pgvector RAG exam-prep",
        "icon": "school",
        "links": [("Open", "https://study.carsonng.com")],
    },
    {
        "name": "Portfolio",
        "desc": "carsonng.com -- AI governance leadership positioning",
        "icon": "badge",
        "links": [("Open", "https://carsonng.com")],
    },
    {
        "name": "AI Regulation Radar",
        "desc": "RegTech compliance tracker -- EU AI Act / NIST / HK PCPD",
        "icon": "gavel",
        "links": [("Open", "https://regtech.carsonng.com")],
    },
    {
        "name": "Change Impact Assessor",
        "desc": "Human-in-the-loop AI risk gate for code/infra changes",
        "icon": "fact_check",
        "links": [("Demo", "https://carsonng.short.gy/change-impact-assessor"),
                  ("GitHub", "https://carsonng.short.gy/change-impact-assessor-github")],
    },
    {
        "name": "Sprint Analyzer",
        "desc": "AI sprint retrospective generator",
        "icon": "assessment",
        "links": [("Demo", "https://carsonng.short.gy/sprint-analyzer"),
                  ("GitHub", "https://carsonng.short.gy/sprint-analyzer-carsonng")],
    },
    {
        "name": "AWS AI Code Review",
        "desc": "Automated PR gate: Amazon Q + Inspector security scanning",
        "icon": "security",
        "links": [("GitHub", "https://carsonng.short.gy/aws-code-review-github")],
    },
]

_STATUS_CACHE: dict = {}


def _probe(url: str) -> bool:
    try:
        resp = httpx.head(url, timeout=5, follow_redirects=True)
        return resp.status_code < 500
    except Exception:
        return False


def refresh_statuses() -> None:
    """Blocking -- call via asyncio.to_thread from async code."""
    for svc in SERVICES:
        up = all(_probe(url) for _, url in svc["links"])
        _STATUS_CACHE[svc["name"]] = {"up": up, "checked_at": time.time()}


def get_status(name: str) -> dict | None:
    return _STATUS_CACHE.get(name)
