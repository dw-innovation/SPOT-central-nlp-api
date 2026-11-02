"""
Dependency health reporting for the Spot NLP API.

The dependencies this service needs (the LLM hub, the tag search and, behind
it, Elasticsearch) all live inside the DW network and cannot be reached from
outside. Without this endpoint their state is invisible: when Elasticsearch
stopped answering, this API stayed up and kept returning instant 422s for
invalid input while every real transformation hung, so any port or /docs check
reported a healthy service for as long as the outage lasted.

Every check is bounded and never raises, because a health endpoint that can
hang is worse than none at all.
"""

import asyncio
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Tuple

import httpx
from loguru import logger

TIMEOUT_SECONDS = float(os.getenv("HEALTH_TIMEOUT_SECONDS", "5"))


async def _run_check(name: str, check: Callable[[], Awaitable[str]]) -> Dict[str, Any]:
    """Run one check, recording its latency and never letting it raise."""
    started = time.monotonic()
    try:
        detail = await check()
        ok = True
    except Exception as exc:  # noqa: BLE001 - a failed check is a result, not an error
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning(f"Health check '{name}' failed: {detail}")

    return {
        "ok": ok,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "detail": detail,
    }


async def _check_tag_search() -> str:
    """
    Look up a single known tag.

    This is the call that returned 500s throughout the Elasticsearch outage,
    so a green result here also means Elasticsearch is answering.
    """
    endpoint = os.getenv("SEARCH_ENDPOINT")
    if not endpoint:
        raise RuntimeError("SEARCH_ENDPOINT is not set")

    async with httpx.AsyncClient(verify=False, timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(
            endpoint, params={"word": "bakery", "limit": 1, "detail": False}
        )
        response.raise_for_status()

    return "tag lookup ok (implies Elasticsearch is answering)"


async def _check_llm_hub() -> str:
    """List the hub's models — proves reachability and credentials, no inference."""
    endpoint = os.getenv("LLMHUB_ENDPOINT")
    if not endpoint:
        raise RuntimeError("LLMHUB_ENDPOINT is not set")

    key = os.getenv("LLMHUB_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.get(
            f"{endpoint.rstrip('/')}/models", headers=headers
        )
        response.raise_for_status()

    return "reachable, credentials accepted (no inference performed)"


async def _check_mongodb(mongo_client: Any) -> str:
    """
    Ping MongoDB in a thread, since pymongo is blocking.

    asyncio.wait_for bounds this: pymongo's own server selection timeout
    defaults to 30s, which is far too long for a health check.
    """
    await asyncio.wait_for(
        asyncio.to_thread(mongo_client.admin.command, "ping"),
        timeout=TIMEOUT_SECONDS,
    )
    return "ping ok"


async def run_health_checks(mongo_client: Any) -> Tuple[Dict[str, Any], bool]:
    """
    Run every dependency check concurrently.

    Returns the report and whether the service should be considered healthy.
    Only critical dependencies decide that: MongoDB is used purely to log
    inference results, and log_to_mongodb already swallows its failures so an
    outage there cannot break a transformation. Reporting it as down would
    take a service that is serving users perfectly well out of rotation.
    """
    checks: Dict[str, Tuple[bool, Callable[[], Awaitable[str]]]] = {
        "tagSearch": (True, _check_tag_search),
        "llmHub": (True, _check_llm_hub),
        "mongodb": (False, lambda: _check_mongodb(mongo_client)),
    }

    started = time.monotonic()
    results = await asyncio.gather(
        *(_run_check(name, check) for name, (_, check) in checks.items())
    )

    report_checks = {}
    for (name, (critical, _)), result in zip(checks.items(), results):
        result["critical"] = critical
        report_checks[name] = result

    healthy = all(r["ok"] for r in report_checks.values() if r["critical"])
    degraded_non_critical = [
        name
        for name, r in report_checks.items()
        if not r["ok"] and not r["critical"]
    ]

    report = {
        "status": "ok" if healthy else "degraded",
        "timestamp": f"{datetime.now():%Y-%m-%dT%H:%M:%S%z}",
        "durationMs": int((time.monotonic() - started) * 1000),
        "model": os.getenv("LLMHUB_MODEL"),
        "checks": report_checks,
    }
    if degraded_non_critical:
        report["warnings"] = degraded_non_critical

    return report, healthy
