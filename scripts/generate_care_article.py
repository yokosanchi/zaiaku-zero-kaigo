#!/usr/bin/env python3
"""Generate a "zero-guilt caregiving" SEO blog article with a Generator-Evaluator loop.

Picks the next keyword from keywords_care.json that has no corresponding
article yet (or a keyword forced via --keyword-id), generates the article
body with an LLM, runs it through deterministic YMYL guardrails, retries
once with feedback on failure, and writes the validated article (plus a
deterministically-appended disclaimer) to src/content/blog/{id}.md.
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

DISCLAIMER = """
---

**この記事について**

本記事は一般的な情報提供を目的としたものであり、個別の医療的・専門的な助言に代わるものではありません。介護に関するお悩みやご不安がある場合は、お住まいの地域の**地域包括支援センター**や、ケアマネジャー、かかりつけの医師などの専門家にご相談ください。介護保険サービスの利用については、市区町村の窓口でも相談を受け付けています。
"""


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


def append_disclaimer(body: str) -> str:
    return body.rstrip() + "\n" + DISCLAIMER + "\n"


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
            final_body = append_disclaimer(body)
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
        print(str(exc))
        return 0

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
