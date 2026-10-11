"""Read-only checks of the README "must-test" items against a running gateway.

Usage: GATEWAY_URL=https://gw.example.com GATEWAY_READ_KEY=... uv run python scripts/live_check.py
"""

from __future__ import annotations

import os
import sys
from urllib.parse import quote

import httpx

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def main() -> int:
    client = httpx.Client(
        base_url=os.environ["GATEWAY_URL"].rstrip("/"),
        headers={"X-Api-Key": os.environ["GATEWAY_READ_KEY"]},
        timeout=120,
    )

    health = client.get("/health").json()
    check("restful backend healthy", health["restful"]["ok"], str(health["restful"].get("error") or ""))
    check("native login + anti-forgery + probe", health["native"]["ok"], str(health["native"].get("error") or ""))

    companies: dict[str, list[dict]] = {}
    for backend in ("restful", "native"):
        response = client.get("/v1/companies", params={"backend": backend})
        ok = response.status_code == 200 and response.headers.get("X-Gateway-Backend") == backend
        companies[backend] = response.json() if ok else []
        check(f"GET /v1/companies via {backend}", ok, f"{len(companies[backend])} companies, HTTP {response.status_code}")

    if companies["restful"] and companies["native"]:
        restful_ids = {c["id"] for c in companies["restful"]}
        native_ids = {c["id"] for c in companies["native"]}
        check(
            "company ids agree across backends",
            restful_ids == native_ids,
            f"only restful: {sorted(restful_ids - native_ids)[:5]}, only native: {sorted(native_ids - restful_ids)[:5]}",
        )

    all_companies = companies["native"] or companies["restful"]
    samples = {
        "quote in name": next((c for c in all_companies if "'" in c["id"]), None),
        "leading/trailing space": next((c for c in all_companies if c["id"] != c["id"].strip()), None),
        "plain name": next((c for c in all_companies if c["id"] == c["id"].strip() and "'" not in c["id"]), None),
    }
    for label, company in samples.items():
        if company is None:
            print(f"[SKIP] no company with {label}")
            continue
        for backend in ("restful", "native"):
            response = client.get(f"/v1/companies/{quote(company['id'], safe='')}/sessions", params={"backend": backend})
            sessions = response.json() if response.status_code == 200 else []
            exact = bool(sessions) and all(s["company"] == company["id"] for s in sessions)
            check(
                f"company sessions ({label}) via {backend}: {company['id']!r}",
                exact,
                f"HTTP {response.status_code}, {len(sessions)} sessions, summary count {company['sessionCount']}",
            )

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    print("Manual: session-expiry re-login (sign the service account out in ScreenConnect, then call any native endpoint).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
