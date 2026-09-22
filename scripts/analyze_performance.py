#!/usr/bin/env python3
"""Weekly SEO performance analysis.

Pulls per-page Search Console data for the site's /qa/ articles,
flags ones with real impressions but a low click-through rate, asks
the LLM for an improved title/description candidate for each (using
the page's own actual ranking queries as grounding), and writes a
Markdown report to scripts/performance_report.md.

This never edits article files directly -- changing the title/
description of an already-indexed page is a real SEO decision, so it
only proposes changes for a human to review and apply. A workflow
step turns the report into a GitHub Issue.

If Search Console isn't configured, or the API call fails, or there's
nothing worth flagging this week, no report file is written and this
exits 0 -- the caller should skip filing an issue in that case.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BLOG_DIR = REPO_ROOT / "src" / "content" / "blog"
REPORT_PATH = REPO_ROOT / "scripts" / "performance_report.md"

LOOKBACK_DAYS = 28
GSC_DATA_LAG_DAYS = 2
MIN_IMPRESSIONS = 10
CTR_THRESHOLD = 0.02  # flag pages under 2% CTR
TOP_N = 10
QUERIES_PER_PAGE = 5


def load_article_frontmatter() -> dict[str, dict]:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from generate_care_article import split_frontmatter

    result: dict[str, dict] = {}
    for f in BLOG_DIR.glob("*.md"):
        raw = f.read_text(encoding="utf-8")
        frontmatter, _, errors = split_frontmatter(raw)
        if errors or frontmatter is None:
            continue
        result[f.stem] = frontmatter
    return result


def slug_from_page_url(url: str) -> str | None:
    parts = [p for p in url.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "qa":
        return parts[-1]
    return None


def date_range() -> tuple[str, str]:
    end_date = dt.date.today() - dt.timedelta(days=GSC_DATA_LAG_DAYS)
    start_date = end_date - dt.timedelta(days=LOOKBACK_DAYS)
    return start_date.isoformat(), end_date.isoformat()


def fetch_page_metrics(service, site_url: str) -> list[dict]:
    start, end = date_range()
    request_body = {
        "startDate": start,
        "endDate": end,
        "dimensions": ["page"],
        "rowLimit": 1000,
    }
    response = service.searchanalytics().query(siteUrl=site_url, body=request_body).execute()
    return response.get("rows", [])


def fetch_top_queries_for_page(service, site_url: str, page_url: str) -> list[str]:
    start, end = date_range()
    request_body = {
        "startDate": start,
        "endDate": end,
        "dimensions": ["query"],
        "dimensionFilterGroups": [
            {"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}
        ],
        "rowLimit": QUERIES_PER_PAGE,
    }
    response = service.searchanalytics().query(siteUrl=site_url, body=request_body).execute()
    return [row["keys"][0] for row in response.get("rows", [])]


def build_suggestion_prompt(current_title: str, current_description: str, queries: list[str], metrics: dict) -> str:
    queries_text = "、".join(queries) if queries else "(データなし)"
    return f"""あなたはSEOのタイトル・メタディスクリプション改善の専門家です。
以下の記事は検索結果に表示されているものの、クリック率が低い状態です。

- 現在のtitle: {current_title}
- 現在のdescription: {current_description}
- 実際にこのページが表示されている検索クエリ: {queries_text}
- 直近{LOOKBACK_DAYS}日間の表示回数: {metrics['impressions']}
- クリック数: {metrics['clicks']}
- クリック率: {metrics['ctr']:.1%}
- 平均掲載順位: {metrics['position']:.1f}

「罪悪感ゼロ介護」という、介護の罪悪感に寄り添うサイトのトーンを保ちながら、
クリックしたくなるtitleとdescriptionを1案ずつ提案してください。
- titleは全角32文字程度まで(検索結果で見切れないように)
- descriptionは全角90〜120文字程度
- 誇大広告的な表現、煽りすぎる表現、断定的な効果の保証は避けること
- 実際の検索クエリの言葉遣いをできるだけ反映すること

出力形式(厳守、説明文なし、JSONオブジェクトのみ):
{{"title": "改善案のtitle", "description": "改善案のdescription", "reason": "改善のポイントを1文で"}}
"""


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from generate_care_article import call_llm, extract_json_object
    from gsc_client import GSCUnavailable, build_service

    try:
        service, site_url = build_service()
    except GSCUnavailable as exc:
        print(f"{exc}; skipping performance report.")
        return 0

    try:
        page_rows = fetch_page_metrics(service, site_url)
    except Exception as exc:  # noqa: BLE001 - never fail the workflow over an API hiccup
        print(f"WARNING: could not fetch Search Console page data: {exc}", file=sys.stderr)
        return 0

    articles = load_article_frontmatter()

    candidates = []
    for row in page_rows:
        page_url = row["keys"][0]
        slug = slug_from_page_url(page_url)
        if slug is None or slug not in articles:
            continue
        impressions = row.get("impressions", 0)
        ctr = row.get("ctr", 0)
        if impressions < MIN_IMPRESSIONS or ctr >= CTR_THRESHOLD:
            continue
        candidates.append(
            {
                "slug": slug,
                "page_url": page_url,
                "impressions": impressions,
                "clicks": row.get("clicks", 0),
                "ctr": ctr,
                "position": row.get("position", 0),
                "frontmatter": articles[slug],
            }
        )

    candidates.sort(key=lambda c: c["impressions"], reverse=True)
    candidates = candidates[:TOP_N]

    if not candidates:
        print("No underperforming pages found this week; not writing a report.")
        return 0

    lines = [
        f"# SEOパフォーマンスレポート ({dt.date.today().isoformat()})",
        "",
        f"直近{LOOKBACK_DAYS}日間のSearch Consoleデータから、表示回数はあるもののクリック率が"
        f"{CTR_THRESHOLD:.0%}未満の記事を抽出し、タイトル・説明文の改善案をAIが提案したものです。"
        "**自動では反映されません。** 良さそうな案があれば、`src/content/blog/{slug}.md` の"
        "frontmatterを手動で更新してください。",
        "",
    ]

    for c in candidates:
        fm = c["frontmatter"]
        current_title = fm.get("title", "")
        current_description = fm.get("description", "")

        try:
            queries = fetch_top_queries_for_page(service, site_url, c["page_url"])
        except Exception:  # noqa: BLE001
            queries = []

        prompt = build_suggestion_prompt(current_title, current_description, queries, c)
        suggestion = None
        try:
            raw = call_llm(
                "あなたはSEO改善の専門家です。指示されたJSON形式を厳守してください。",
                [{"role": "user", "content": prompt}],
            )
            suggestion = json.loads(extract_json_object(raw))
        except Exception as exc:  # noqa: BLE001 - one bad suggestion shouldn't kill the whole report
            print(f"WARNING: suggestion generation failed for {c['slug']}: {exc}", file=sys.stderr)

        lines.append(f"## {c['slug']}")
        lines.append("")
        lines.append(f"- ページ: {c['page_url']}")
        lines.append(
            f"- 表示回数: {c['impressions']} / クリック数: {c['clicks']} / "
            f"CTR: {c['ctr']:.1%} / 平均順位: {c['position']:.1f}"
        )
        lines.append(f"- 実際の検索クエリ: {'、'.join(queries) if queries else '(データなし)'}")
        lines.append("")
        lines.append(f"**現在のtitle**: {current_title}")
        lines.append(f"**現在のdescription**: {current_description}")
        lines.append("")
        if suggestion:
            lines.append(f"**改善案title**: {suggestion.get('title', '(生成失敗)')}")
            lines.append(f"**改善案description**: {suggestion.get('description', '(生成失敗)')}")
            lines.append(f"**改善のポイント**: {suggestion.get('reason', '')}")
        else:
            lines.append("_改善案の生成に失敗しました。_")
        lines.append("")

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote performance report for {len(candidates)} page(s) to {REPORT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
