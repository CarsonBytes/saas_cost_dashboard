"""Access log tracking for the Command Deck dashboard.

Records every page load with IP, region, user agent, referer, and timestamp.
Geo-resolution uses ip-api.com (free, no key, 45 req/min limit) with an
in-memory cache to avoid re-resolving the same IP.
"""
import os
import time
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
_SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
_TABLE = "access_log"

# In-memory cache: IP -> (region, timestamp).  Entries older than 24h are
# re-resolved (ip-api.com is free but rate-limited; caching per-IP is safe
# because public IPs rarely change within a day).
_geo_cache: dict[str, tuple[str, float]] = {}
_GEO_CACHE_TTL = 86400  # 24 hours

# Bot detection tokens (same list as app.py)
_BOT_UA_TOKENS = ("bot", "crawl", "spider", "slurp", "curl", "wget",
                  "python-requests", "httpx", "scrapy", "headless",
                  "preview", "monitor", "uptime", "facebookexternalhit")


def _is_bot(user_agent: str | None) -> bool:
    if not user_agent:
        return True
    ua = user_agent.lower()
    return any(t in ua for t in _BOT_UA_TOKENS)


def _resolve_region(ip: str | None) -> str:
    """Resolve IP to region via ip-api.com.  Returns 'Unknown' on failure.
    Does NOT cache 'Unknown' so transient failures can retry on next request."""
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return "Local"
    now = time.monotonic()
    cached = _geo_cache.get(ip)
    if cached and cached[0] != "Unknown" and now - cached[1] < _GEO_CACHE_TTL:
        return cached[0]
    try:
        resp = httpx.get(
            f"http://ip-api.com/json/{ip}?fields=country,regionName,city",
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                parts = [data.get("city"), data.get("regionName"), data.get("country")]
                region = ", ".join(p for p in parts if p) or "Unknown"
                _geo_cache[ip] = (region, now)
                return region
    except Exception:  # noqa: BLE001
        pass
    # Don't cache Unknown -- next page load will retry
    return "Unknown"


def _get_client_ip(request) -> str | None:
    """Extract client IP from a FastAPI/Starlette request object."""
    # Check X-Forwarded-For first (behind reverse proxy)
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    # Check X-Real-IP
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    # Fall back to direct connection
    if hasattr(request, "client") and request.client:
        return request.client.host
    return None


def record_access(request) -> None:
    """Record a page load to the access_log table.  Fire-and-forget."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return
    try:
        ip = _get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")
        referer = request.headers.get("referer", "")
        path = str(request.url.path) if hasattr(request, "url") else "/"
        method = request.method if hasattr(request, "method") else "GET"
        is_bot = _is_bot(user_agent)
        region = _resolve_region(ip) if not is_bot else "Bot"

        row = {
            "path": path,
            "method": method,
            "ip": ip,
            "region": region,
            "user_agent": user_agent[:500],  # truncate long UAs
            "referer": referer[:500],
            "is_bot": is_bot,
        }
        httpx.post(
            f"{_SUPABASE_URL.rstrip('/')}/rest/v1/{_TABLE}",
            json=row,
            headers={"apikey": _SUPABASE_KEY, "Authorization": f"Bearer {_SUPABASE_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            timeout=5,
        )
    except Exception:  # noqa: BLE001
        log.debug("access_log record failed", exc_info=True)


def fetch_access_log(since: str | None = None, until: str | None = None,
                     limit: int = 200) -> list[dict]:
    """Read recent access log entries from Supabase."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return []
    params = [("select", "*"), ("order", "ts.desc"), ("limit", str(limit))]
    if since:
        params.append(("ts", f"gte.{since}"))
    if until:
        params.append(("ts", f"lt.{until}"))
    try:
        resp = httpx.get(
            f"{_SUPABASE_URL.rstrip('/')}/rest/v1/{_TABLE}",
            params=params,
            headers={"apikey": _SUPABASE_KEY, "Authorization": f"Bearer {_SUPABASE_KEY}"},
            timeout=10,
        )
        if resp.status_code in (200, 206):
            return resp.json() or []
    except Exception:  # noqa: BLE001
        log.debug("access_log fetch failed", exc_info=True)
    return []


_LOCALHOST_IPS = {"127.0.0.1", "::1", "localhost"}


def fetch_access_stats(days: int = 7, exclude_localhost: bool = True, exclude_bots: bool = True) -> dict:
    """Aggregate access log stats for the dashboard."""
    import datetime as _dt
    since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()
    all_rows = fetch_access_log(since=since, limit=10000)
    if exclude_localhost:
        all_rows = [r for r in all_rows if (r.get("ip") or "") not in _LOCALHOST_IPS]
    # Always count from full set for KPI accuracy
    total_all = len(all_rows)
    bots = sum(1 for r in all_rows if r.get("is_bot"))
    humans = total_all - bots
    # Filter for charts/tables
    rows = [r for r in all_rows if not r.get("is_bot")] if exclude_bots else all_rows

    # Hourly breakdown for chart
    hourly: dict[str, dict] = {}
    for r in rows:
        ts = r.get("ts", "")
        hour = ts[:13] if ts else "?"
        hourly.setdefault(hour, {"human": 0, "bot": 0})
        if r.get("is_bot"):
            hourly[hour]["bot"] += 1
        else:
            hourly[hour]["human"] += 1

    # Region breakdown
    regions: dict[str, int] = {}
    for r in rows:
        if not r.get("is_bot"):
            reg = r.get("region") or "Unknown"
            regions[reg] = regions.get(reg, 0) + 1

    # Path popularity (human only)
    path_counts: dict[str, int] = {}
    for r in rows:
        p = r.get("path") or "/"
        path_counts[p] = path_counts.get(p, 0) + 1

    # Visits per unique human IP (from full set, not bot-filtered)
    human_ips = set(r.get("ip") for r in all_rows if not r.get("is_bot") and r.get("ip"))
    avg_visits = round(humans / max(len(human_ips), 1), 1)

    return {
        "total": total_all,
        "bots": bots,
        "humans": humans,
        "unique_ips": len(human_ips),
        "avg_visits_per_ip": avg_visits,
        "top_paths": sorted(path_counts.items(), key=lambda x: -x[1])[:10],
        "hourly": hourly,
        "regions": regions,
        "recent": rows[:50],
    }
