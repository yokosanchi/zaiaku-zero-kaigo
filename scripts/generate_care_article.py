#!/usr/bin/env python3
"""Generate a "zero-guilt caregiving" SEO blog article with a Generator-Evaluator loop.

Picks the next keyword from keywords_care.json that has no corresponding
article yet (or a keyword forced via --keyword-id). If every keyword already
has an article, asks the LLM to brainstorm a fresh batch of long-tail
keywords in the site's existing categories, validates and appends the ones
that pass (id format, no duplicates, allowed category/conversion_type) back
to keywords_care.json, and picks one of those instead -- so the pipeline
never runs out of topics without human intervention. Either way, it then
generates the article body with an LLM, runs it through deterministic YMYL
guardrails, retries once with feedback on failure, and writes the validated
article (plus a deterministically-appended references + disclaimer section)
to src/content/blog/{id}.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_PATH = REPO_ROOT / "scripts" / "keywords_care.json"
BLOG_DIR = REPO_ROOT / "src" / "content" / "blog"

MIN_CHARS = 2500
MAX_CHARS = 4500
MIN_H2 = 2
MAX_H2 = 7
MAX_ATTEMPTS = 2  # 1 initial generation + 1 feedback-driven retry

# 医療的・断定的な表現。YMYL領域での過度な断定・誤解を招く表現を禁止する。
FORBIDDEN_PHRASES = [
    "治る",
    "完治",
    "100%改善",
    "必ず効く",
    "医学的診断",
]

REQUIRED_FRONTMATTER_KEYS = [
    "title",
    "description",
    "pubDate",
    "keyword",
    "category",
    "target_searcher",
    "conversion_type",
]

DISCLAIMER_HEADER = """
---

**この記事について**

本記事は一般的な情報提供を目的としたものであり、個別の医療的・専門的な助言に代わるものではありません。介護に関するお悩みやご不安がある場合は、お住まいの地域の**地域包括支援センター**や、ケアマネジャー、かかりつけの医師などの専門家にご相談ください。介護保険サービスの利用については、市区町村の窓口でも相談を受け付けています。
"""

# 公的機関・公的な情報源。LLMに出典を自由に書かせるとハルシネーション
# (存在しない文書・URLの捏造)のリスクがあるため、カテゴリごとに
# 事前に検証済みの実在する情報源だけを機械的に付与する。
REFERENCE_SOURCES: dict[str, list[tuple[str, str]]] = {
    "施設入所": [
        ("介護サービス情報公表システム(厚生労働省)", "https://www.kaigokensaku.mhlw.go.jp/"),
        ("WAM NET(福祉医療機構)", "https://www.wam.go.jp/"),
    ],
    "心理的負担": [
        ("こころの耳(厚生労働省 働く人のメンタルヘルス・ポータルサイト)", "https://kokoro.mhlw.go.jp/"),
        ("厚生労働省", "https://www.mhlw.go.jp/"),
    ],
    "サービス利用": [
        ("介護サービス情報公表システム(厚生労働省)", "https://www.kaigokensaku.mhlw.go.jp/"),
        ("WAM NET(福祉医療機構)", "https://www.wam.go.jp/"),
    ],
    "家族関係": [
        ("WAM NET(福祉医療機構)", "https://www.wam.go.jp/"),
        ("厚生労働省", "https://www.mhlw.go.jp/"),
    ],
    "離職": [
        ("厚生労働省", "https://www.mhlw.go.jp/"),
        ("こころの耳(厚生労働省 働く人のメンタルヘルス・ポータルサイト)", "https://kokoro.mhlw.go.jp/"),
    ],
    "ショートステイ": [
        ("介護サービス情報公表システム(厚生労働省)", "https://www.kaigokensaku.mhlw.go.jp/"),
        ("WAM NET(福祉医療機構)", "https://www.wam.go.jp/"),
    ],
    "認知症ケア": [
        ("厚生労働省", "https://www.mhlw.go.jp/"),
        ("WAM NET(福祉医療機構)", "https://www.wam.go.jp/"),
    ],
    "訪問介護": [
        ("介護サービス情報公表システム(厚生労働省)", "https://www.kaigokensaku.mhlw.go.jp/"),
        ("WAM NET(福祉医療機構)", "https://www.wam.go.jp/"),
    ],
    "家族間トラブル": [
        ("法テラス(日本司法支援センター)", "https://www.houterasu.or.jp/"),
        ("全国社会福祉協議会", "https://www.shakyo.or.jp/"),
    ],
    "セルフケア": [
        ("こころの耳(厚生労働省 働く人のメンタルヘルス・ポータルサイト)", "https://kokoro.mhlw.go.jp/"),
        ("厚生労働省", "https://www.mhlw.go.jp/"),
    ],
}

DEFAULT_REFERENCES: list[tuple[str, str]] = [
    ("厚生労働省", "https://www.mhlw.go.jp/"),
    ("WAM NET(福祉医療機構)", "https://www.wam.go.jp/"),
]

# キーワード自動補充で許容するカテゴリ・コンバージョン種別。
# REFERENCE_SOURCES のキーが既存カテゴリの正とする(表示側のアイコン・色
# マッピングと一致させるため、LLMにはこの範囲内でしか選ばせない)。
ALLOWED_CATEGORIES: list[str] = list(REFERENCE_SOURCES.keys())
ALLOWED_CONVERSION_TYPES: list[str] = ["施設検索", "資料請求", "訪問介護マッチング"]
KEYWORD_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NEW_KEYWORDS_PER_BATCH = 5


class GenerationError(RuntimeError):
    pass


def load_keywords() -> list[dict]:
    with KEYWORDS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def pick_keyword(keywords: list[dict], forced_id: str | None) -> dict:
    if forced_id:
        for kw in keywords:
            if kw["id"] == forced_id:
                return kw
        raise GenerationError(f"keyword id '{forced_id}' not found in {KEYWORDS_PATH}")

    for kw in keywords:
        article_path = BLOG_DIR / f"{kw['id']}.md"
        if not article_path.exists():
            return kw

    raise GenerationError("no pending keywords: all articles already generated")


SEARCH_CONSOLE_INSIGHTS_PATH = REPO_ROOT / "scripts" / "search_console_insights.json"


def load_search_console_insights() -> list[dict]:
    if not SEARCH_CONSOLE_INSIGHTS_PATH.exists():
        return []
    try:
        with SEARCH_CONSOLE_INSIGHTS_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def build_keyword_gen_prompt(existing_keywords: list[dict], search_console_insights: list[dict]) -> str:
    existing_list = "\n".join(f"- {kw['keyword']}（{kw['category']}）" for kw in existing_keywords)
    categories = "、".join(ALLOWED_CATEGORIES)
    conversion_types = "、".join(ALLOWED_CONVERSION_TYPES)

    if search_console_insights:
        insight_lines = "\n".join(
            f"- {row['query']}（表示回数: {row['impressions']}）" for row in search_console_insights
        )
        demand_section = f"""
# 実際の検索データ(Google Search Console、直近28日間)
以下は、このサイトが実際に検索結果に表示されているが、まだ専用記事がないクエリです。
これらは実在する検索需要なので、できる限りこの中から選んでキーワード候補にしてください。
{insight_lines}
"""
    else:
        demand_section = ""

    return f"""あなたは「罪悪感ゼロ介護」というサイトのSEOキーワードリサーチ担当です。
このサイトは、介護のなかで生まれる罪悪感(施設入所、自分の時間を優先すること等)を、
介護福祉士・社会福祉士の視点から肯定し、悩みを抱える家族介護者に寄り添うQ&A形式の
情報サイトです。

# 既存のキーワード(重複や似すぎた切り口を避けること)
{existing_list}
{demand_section}
# 依頼内容
上記とは異なる具体的な悩み・検索意図を持つ、ロングテールキーワードを{NEW_KEYWORDS_PER_BATCH}個考えてください。
- 上の「実際の検索データ」があれば、それを優先的にキーワード化すること(実証済みの検索需要があるため)。データがない、または{NEW_KEYWORDS_PER_BATCH}個に満たない場合は、残りを自分で考えて補うこと。
- 実際に介護中の家族が検索しそうな、自然で具体的な日本語のフレーズにすること。
- 「介護 罪悪感」のような一般語だけでなく、具体的な状況(誰の・どんな場面での・どんな感情か)を含めること。
- カテゴリは必ず次の中から1つを選ぶこと: {categories}
- conversion_typeは必ず次の中から1つを選ぶこと: {conversion_types}
- idは、keywordをローマ字化したような半角英数字とハイフンのみの一意な文字列にすること(スペースや日本語を含めない)。

# 出力形式(厳守)
説明文や前置きは一切書かず、以下のJSON配列だけを出力してください。
[
  {{
    "id": "半角英数字とハイフンのみのid",
    "keyword": "検索されそうな日本語キーワード",
    "category": "上記カテゴリのいずれか",
    "target_searcher": "この記事を検索しそうな人物像の説明",
    "conversion_type": "上記のいずれか"
  }}
]
"""


def extract_json_array(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(json)?", "", cleaned.strip())
    cleaned = re.sub(r"```$", "", cleaned.strip())
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise GenerationError(f"keyword generation did not return a JSON array: {text[:200]!r}")
    return cleaned[start : end + 1]


def validate_keyword_candidate(
    entry: object, existing_ids: set[str], existing_keyword_texts: set[str]
) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["キーワード候補がオブジェクト形式ではありません"]

    required = ["id", "keyword", "category", "target_searcher", "conversion_type"]
    missing = [k for k in required if not entry.get(k)]
    if missing:
        errors.append(f"必須キーが不足しています: {', '.join(missing)}")
        return errors

    if not KEYWORD_ID_PATTERN.match(entry["id"]):
        errors.append(f"idの形式が不正です(半角英数字とハイフンのみ): {entry['id']!r}")
    elif entry["id"] in existing_ids:
        errors.append(f"idが既存のものと重複しています: {entry['id']!r}")

    if entry["keyword"] in existing_keyword_texts:
        errors.append(f"keywordが既存のものと重複しています: {entry['keyword']!r}")

    if entry["category"] not in ALLOWED_CATEGORIES:
        errors.append(f"categoryが許容範囲外です: {entry['category']!r}")

    if entry["conversion_type"] not in ALLOWED_CONVERSION_TYPES:
        errors.append(f"conversion_typeが許容範囲外です: {entry['conversion_type']!r}")

    return errors


def generate_new_keywords(existing_keywords: list[dict]) -> list[dict]:
    insights = load_search_console_insights()
    if insights:
        print(f"Using {len(insights)} Search Console query insight(s) to guide keyword generation.")
    prompt = build_keyword_gen_prompt(existing_keywords, insights)
    raw = call_llm(
        "あなたはSEOキーワードリサーチのアシスタントです。指示された形式を厳守してください。",
        [{"role": "user", "content": prompt}],
    )
    json_text = extract_json_array(raw)
    try:
        candidates = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise GenerationError(f"keyword generation returned invalid JSON: {exc}") from exc

    if not isinstance(candidates, list):
        raise GenerationError("keyword generation did not return a JSON array")

    existing_ids = {kw["id"] for kw in existing_keywords}
    existing_keyword_texts = {kw["keyword"] for kw in existing_keywords}

    accepted: list[dict] = []
    for candidate in candidates:
        errors = validate_keyword_candidate(candidate, existing_ids, existing_keyword_texts)
        if errors:
            print(f"[keyword-gen] rejected candidate {candidate!r}: {errors}", file=sys.stderr)
            continue
        accepted.append(
            {
                "id": candidate["id"],
                "keyword": candidate["keyword"],
                "category": candidate["category"],
                "target_searcher": candidate["target_searcher"],
                "conversion_type": candidate["conversion_type"],
            }
        )
        existing_ids.add(candidate["id"])
        existing_keyword_texts.add(candidate["keyword"])

    return accepted


def save_keywords(keywords: list[dict]) -> None:
    with KEYWORDS_PATH.open("w", encoding="utf-8") as f:
        json.dump(keywords, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_system_prompt() -> str:
    return """あなたは「介護福祉士」および「社会福祉士」の資格に相当する知見を持つ、共感力の高いWebライターです。
あなたの専門は「罪悪感ゼロ介護」というコンセプトで、家族介護者が抱える罪悪感を和らげる記事を書くことです。

# 執筆方針(最重要)
- 施設入所、デイサービスやショートステイの利用、訪問介護への依頼、そして「自分の時間を優先すること」は、介護者にとって正当な選択であり、決して親不孝や手抜きではないという立場を全面的に肯定してください。
- 読者を否定せず、「あなたは十分頑張っている」というメッセージを一貫して伝えてください。
- 精神論だけでなく、地域包括支援センターやケアマネジャー、介護保険サービスなど、現実的に頼れる社会資源を具体的に紹介してください。

# YMYL(Your Money or Your Life)ガードレール(厳守)
- 医療行為や病状について断定的な診断・予後を述べないこと。
- 「治る」「完治」「100%改善」「必ず効く」「医学的診断」等、効果や治癒を保証・断定する表現は絶対に使用しないこと。
- あくまで一般的な情報提供である旨を意識した、誠実で控えめな表現を用いること。
- 「専門家に相談してください」「地域包括支援センターに相談しましょう」といった相談の呼びかけは、記事全体を通して1〜2箇所程度の自然な言及にとどめること。免責事項は本文とは別に機械的に末尾へ付与されるため、本文中で何度も繰り返す必要はない。記事自体が、読者に具体的な考え方の転換や行動のヒントを与えることを最優先とする。

# 文体・読みやすさ(重要)
読者は、疲れ切った状態でスマートフォンから読んでいる介護者です。専門家としての信頼感は保ちつつ、内容が頭に入ってきやすい文章にしてください。
- 1文は短く。目安として、40〜60字を超える一文が連続しないようにすること。
- 1段落は2〜4文程度で区切り、こまめに改行(空行)を入れること。壁のような長い段落は禁止。
- 手順や具体的な行動、相談先の候補など、列挙できる内容は箇条書き(「- 」)を積極的に使うこと。
- 「〜であると考えられます」「〜することが望ましい」のような硬い論文調は避け、「〜です」「〜してみませんか」「〜なんです」のような、目の前の相手に語りかけるような、やさしい口調にすること。
- ただし、相談先の名称(地域包括支援センター等)や制度名、YMYLに関わる注意点は、口調がやわらかくても内容は正確に書くこと。

# フォーマット要件(厳守)
- 出力はMarkdownファイル全体とする。
- 先頭にYAML Frontmatterを ``---`` で囲んで記載し、以下のキーを必ず含めること:
  title, description, pubDate, keyword, category, target_searcher, conversion_type
- Frontmatterの直後、本文の先頭に「# 」で始まるH1見出しを1つだけ記載すること(記事タイトル)。
- 本文中に「## 」で始まるH2見出しを4〜6個使い、論理的なセクション構成にすること。「よくある質問」「周囲の人ができること」のような付け足し的なセクションを増やして見出し数を水増ししないこと。1つ1つのセクションにしっかり内容を持たせ、細切れの箇条書きの寄せ集めにしないこと。
- 本文(Frontmatterを除く)の文字数は日本語で2500文字〜4500文字の範囲に収めること。
- 免責事項や地域包括支援センターへの相談を促す文言は自動的に別途付与されるため、本文の最後に自分で書く必要はない。
"""


def build_user_prompt(keyword: dict, pub_date: str) -> str:
    return f"""次の情報に基づいて、罪悪感ゼロ介護をテーマにしたSEOブログ記事を1本、Markdown形式で作成してください。

- 対策キーワード: {keyword['keyword']}
- カテゴリ: {keyword['category']}
- 想定読者: {keyword['target_searcher']}
- 記事の主なコンバージョン導線: {keyword['conversion_type']}
- pubDate: {pub_date}

Frontmatterの id には "{keyword['id']}" を使わず、title/description/pubDate/keyword/category/target_searcher/conversion_type のみを含めてください。
"""


def build_retry_prompt(errors: list[str]) -> str:
    bullet_errors = "\n".join(f"- {e}" for e in errors)
    return f"""前回生成した記事は以下の検証エラーにより不合格でした。指摘点をすべて修正し、記事全体(Frontmatter込み)を再度Markdown形式で出力し直してください。

{bullet_errors}

修正版は必ずFrontmatterから始まる完全なMarkdown文書として出力してください。
"""


def call_llm(system_prompt: str, messages: list[dict]) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise GenerationError(
            "google-genai package is not installed. Run `pip install -r scripts/requirements.txt`."
        ) from exc

    import os

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise GenerationError("GEMINI_API_KEY environment variable is not set")

    # Free-tier-eligible model via Google AI Studio (ai.google.dev). Override
    # with GEMINI_MODEL if the free-tier lineup changes.
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    client = genai.Client(api_key=api_key)

    contents = [
        types.Content(
            role="model" if m["role"] == "assistant" else "user",
            parts=[types.Part(text=m["content"])],
        )
        for m in messages
    ]

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )
    return response.text


def split_frontmatter(raw: str) -> tuple[dict | None, str, list[str]]:
    errors: list[str] = []
    text = raw.strip()
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        errors.append("YAML Frontmatterが見つかりません（先頭が --- ... --- 形式である必要があります）")
        return None, text, errors

    fm_text, body = match.group(1), match.group(2)
    try:
        frontmatter = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        errors.append(f"Frontmatterのパースに失敗しました: {exc}")
        return None, body, errors

    if not isinstance(frontmatter, dict):
        errors.append("Frontmatterがオブジェクト（key: value）形式ではありません")
        return None, body, errors

    return frontmatter, body, errors


def evaluate_article(raw: str) -> tuple[bool, list[str], dict | None, str]:
    errors: list[str] = []
    frontmatter, body, fm_errors = split_frontmatter(raw)
    errors.extend(fm_errors)

    if frontmatter is not None:
        missing = [k for k in REQUIRED_FRONTMATTER_KEYS if k not in frontmatter or not frontmatter[k]]
        if missing:
            errors.append(f"Frontmatterに必須キーが不足しています: {', '.join(missing)}")

    for phrase in FORBIDDEN_PHRASES:
        if phrase in body:
            errors.append(f"禁止語が検出されました: 「{phrase}」")

    h1_matches = re.findall(r"(?m)^#\s+.+$", body)
    h2_matches = re.findall(r"(?m)^##\s+.+$", body)
    if len(h1_matches) != 1:
        errors.append(f"H1見出し(# )は1つである必要がありますが、{len(h1_matches)}個検出されました")
    if not (MIN_H2 <= len(h2_matches) <= MAX_H2):
        errors.append(
            f"H2見出し(## )は{MIN_H2}〜{MAX_H2}個である必要がありますが、{len(h2_matches)}個検出されました"
            "（見出しを増やしすぎず、1セクションにしっかり内容を持たせてください）"
        )

    char_count = len(body.strip())
    if not (MIN_CHARS <= char_count <= MAX_CHARS):
        errors.append(
            f"本文の文字数が範囲外です: {char_count}文字（許容範囲: {MIN_CHARS}〜{MAX_CHARS}文字）"
        )

    passed = len(errors) == 0
    return passed, errors, frontmatter, body


def build_references_block(category: str) -> str:
    sources = REFERENCE_SOURCES.get(category, DEFAULT_REFERENCES)
    links = "\n".join(f"- [{name}]({url})" for name, url in sources)
    return f"""
---

**参考情報**

{links}
"""


def append_disclaimer(body: str, category: str) -> str:
    return body.rstrip() + "\n" + build_references_block(category) + DISCLAIMER_HEADER + "\n"


def assemble_markdown(frontmatter: dict, body: str) -> str:
    fm_yaml = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm_yaml}\n---\n\n{body.strip()}\n"


def generate_article(keyword: dict) -> str:
    pub_date = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
    system_prompt = build_system_prompt()
    messages = [{"role": "user", "content": build_user_prompt(keyword, pub_date)}]

    last_errors: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        raw = call_llm(system_prompt, messages)
        passed, errors, frontmatter, body = evaluate_article(raw)

        if passed:
            assert frontmatter is not None
            frontmatter.setdefault("keyword", keyword["keyword"])
            frontmatter.setdefault("category", keyword["category"])
            frontmatter.setdefault("target_searcher", keyword["target_searcher"])
            frontmatter.setdefault("conversion_type", keyword["conversion_type"])
            frontmatter["pubDate"] = pub_date
            final_body = append_disclaimer(body, keyword["category"])
            return assemble_markdown(frontmatter, final_body)

        last_errors = errors
        print(f"[attempt {attempt}] validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)

        if attempt < MAX_ATTEMPTS:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": build_retry_prompt(errors)})

    raise GenerationError(
        "article failed validation after retry: " + "; ".join(last_errors)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keyword-id",
        default=None,
        help="Force generation for a specific keyword id instead of picking the next pending one.",
    )
    args = parser.parse_args()

    keywords = load_keywords()
    try:
        keyword = pick_keyword(keywords, args.keyword_id)
    except GenerationError as exc:
        if args.keyword_id is not None:
            print(str(exc))
            return 0

        print("No pending keywords. Asking the LLM to brainstorm new ones...")
        try:
            new_entries = generate_new_keywords(keywords)
        except GenerationError as gen_exc:
            print(f"ERROR: could not auto-generate new keywords: {gen_exc}", file=sys.stderr)
            return 1

        if not new_entries:
            print("Keyword auto-generation produced no valid new candidates this run.")
            return 0

        keywords.extend(new_entries)
        save_keywords(keywords)
        print(
            f"Added {len(new_entries)} new keyword(s) to "
            f"{KEYWORDS_PATH.relative_to(REPO_ROOT)}: "
            + ", ".join(k["id"] for k in new_entries)
        )
        keyword = pick_keyword(keywords, None)

    print(f"Generating article for keyword id='{keyword['id']}' ({keyword['keyword']})")

    try:
        markdown = generate_article(keyword)
    except GenerationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    BLOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BLOG_DIR / f"{keyword['id']}.md"
    out_path.write_text(markdown, encoding="utf-8")
    print(f"Wrote {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
