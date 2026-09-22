#!/usr/bin/env python3
"""Fetch real search queries from Google Search Console.

Surfaces queries that already have measurable impressions but no
dedicated article yet, so generate_care_article.py's keyword
brainstorming can prioritize genuine, proven search demand instead of
guessing blind. Writes scripts/search_console_insights.json (a
transient cache, not committed to git).

Requires GSC_SERVICE_ACCOUNT_KEY (the service account's JSON key, as a
single-line JSON string) and GSC_SITE_URL (the verified Search Console
property URL, e.g. https://zaiaku-zero-kaigo.yokosanchi.workers.dev/)
as environment variables. If either is missing, or the API call fails
for any reason (auth, quota, network), this writes an empty insights
file and exits 0 -- Search Console data is a nice-to-have prioritization
signal, never a hard dependency for the publishing pipeline.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_PATH = REPO_ROOT / "scripts" / "keywords_care.json"
INSIGHTS_PATH = REPO_ROOT / "scripts" / "search_console_insights.json"

LOOKBACK_DAYS = 28
GSC_DATA_LAG_DAYS = 2  # Search Console data is not real-time
MIN_IMPRESSIONS = 3
TOP_N = 15


def load_existing_keyword_texts() -> set[str]:
    with KEYWORDS_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return {normalize(kw["keyword"]) for kw in data}


def normalize(text: str) -> str:
    return "".join(text.split()).lower()


def fetch_query_rows(service, site_url: str) -> list[dict]:
    end_date = dt.date.today() - dt.timedelta(days=GSC_DATA_LAG_DAYS)
    start_date = end_date - dt.timedelta(days=LOOKBACK_DAYS)

    request_body = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "dimensions": ["query"],
        "rowLimit": 250,
    }
    response = (
        service.searchanalytics()
        .query(siteUrl=site_url, body=request_body)
        .execute()
    )
    return response.get("rows", [])


def write_insights(rows: list[dict]) -> None:
    INSIGHTS_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gsc_client import GSCUnavailable, build_service

    try:
        service, site_url = build_service()
    except GSCUnavailable as exc:
        print(f"{exc}; skipping Search Console fetch.")
        write_insights([])
        return 0

    try:
        rows = fetch_query_rows(service, site_url)
    except Exception as exc:  # noqa: BLE001 - any auth/network/quota failure must not break publishing
        print(f"WARNING: Search Console fetch failed, continuing without it: {exc}", file=sys.stderr)
        write_insights([])
        return 0

    existing = load_existing_keyword_texts()

    candidates = []
    for row in rows:
        query = row["keys"][0]
        impressions = row.get("impressions", 0)
        if impressions < MIN_IMPRESSIONS:
            continue
        if normalize(query) in existing:
            continue
        candidates.append(
            {
                "query": query,
                "impressions": impressions,
                "clicks": row.get("clicks", 0),
                "ctr": row.get("ctr", 0),
                "position": row.get("position", 0),
            }
        )

    candidates.sort(key=lambda c: c["impressions"], reverse=True)
    top = candidates[:TOP_N]

    write_insights(top)
    print(f"Wrote {len(top)} Search Console query insight(s) to {INSIGHTS_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
