from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "state" / "state.json"
CACHE_FILE = ROOT / "data" / "translations.json"
OUTPUT_DIR = ROOT / "outputs-public"
RUNTIME_DIR = ROOT / "runtime"

FEED_BASE = "https://rss.arxiv.org/atom/"
API_URL = "https://export.arxiv.org/api/query"
PROMPT_VERSION = "2026-09-01.v2"
ARXIV_MIN_INTERVAL_SECONDS = 3.2

CATEGORY_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
VERSION_SUFFIX = re.compile(r"v\d+$")
SPACE_PATTERN = re.compile(r"\s+")
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

ATOM = "http://www.w3.org/2005/Atom"
ARXIV = "http://arxiv.org/schemas/atom"
DC = "http://purl.org/dc/elements/1.1/"
NS = {"atom": ATOM, "arxiv": ARXIV, "dc": DC}

CATEGORY_LABELS = {
    "hep-th": "高能理论",
    "hep-ph": "高能唯象",
    "hep-lat": "格点量子色动力学",
    "hep-ex": "高能实验",
    "nucl-th": "核理论",
    "nucl-ex": "核实验",
    "gr-qc": "广义相对论与量子宇宙学",
    "astro-ph": "天体物理",
    "quant-ph": "量子物理",
}

PAGE_CSS = """
    :root { color-scheme: light; font-family: Inter, "Noto Sans SC", system-ui, sans-serif; }
    body { margin: 0; background: #f4f6f8; color: #1f2933; line-height: 1.7; }
    header, main, footer { width: min(980px, calc(100% - 32px)); margin: auto; }
    header { padding: 40px 0 18px; }
    h1 { margin: 0 0 8px; line-height: 1.25; }
    .lede { color: #465563; margin: 0; }
    nav { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
    nav a, .tag { border-radius: 999px; background: #e7eef8; padding: 3px 10px; text-decoration: none; color: #244c7a; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; margin: 24px 0 40px; }
    .card { display: block; background: white; border-radius: 14px; padding: 22px 22px 18px; text-decoration: none; color: inherit; box-shadow: 0 5px 18px rgba(31,41,51,.07); border: 1px solid transparent; }
    a.card:hover { border-color: #3266a8; }
    .card.disabled { opacity: .62; cursor: default; box-shadow: none; }
    .card-code { font-size: .95rem; color: #3266a8; font-weight: 650; }
    .card-name { margin-top: 4px; color: #465563; }
    .card-count { margin-top: 18px; font-size: 2rem; font-weight: 700; line-height: 1; }
    .card-count span { font-size: 1rem; font-weight: 500; color: #667; margin-left: 4px; }
    .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 22px 0 8px; }
    .stat { background: white; border-radius: 12px; padding: 14px 16px; box-shadow: 0 5px 18px rgba(31,41,51,.07); }
    .stat-label { color: #667; font-size: .9rem; }
    .stat-value { font-size: 1.7rem; font-weight: 700; line-height: 1.2; }
    .toc { background: white; border-radius: 14px; padding: 20px 24px 10px; margin: 22px 0; box-shadow: 0 5px 18px rgba(31,41,51,.07); }
    .toc h2 { margin: 0 0 8px; }
    .toc h3 { margin: 16px 0 6px; font-size: 1.05rem; color: #244c7a; }
    .toc-list { padding-left: 22px; }
    .toc-list a { color: #1f2933; }
    .toc-list small { color: #667; }
    .section-title { margin: 36px 0 0; }
    .paper { background: white; border-radius: 14px; padding: 24px; margin: 18px 0; box-shadow: 0 5px 18px rgba(31,41,51,.07); }
    .paper-head { display: flex; justify-content: space-between; gap: 18px; align-items: baseline; }
    .paper h2 { margin: 0; line-height: 1.35; }
    .paper h3 { border-left: 4px solid #3266a8; padding-left: 10px; margin-top: 24px; }
    .paper h4 { margin-bottom: 0; color: #345; }
    .paper p { margin-top: 7px; }
    .original-title { color: #566574; font-style: italic; }
    .meta { color: #465563; }
    .tags { display: flex; flex-wrap: wrap; gap: 6px; }
    .tag { font-size: .85rem; }
    .badge { display: inline-block; margin-left: 8px; font-size: .75rem; border-radius: 999px; padding: 1px 8px; background: #e7eef8; color: #244c7a; vertical-align: middle; }
    .evaluation { background: #f7f9fc; border-radius: 10px; padding: 1px 16px 10px; }
    .warning { color: #8a3d00; background: #fff4e5; border-radius: 8px; padding: 8px 12px; }
    footer { padding: 24px 0 46px; color: #667; }
    @media (max-width: 640px) {
      .paper { padding: 18px; }
      .paper-head { display: block; }
      .stats { grid-template-columns: 1fr; }
    }
"""


def env_text(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def env_int(name: str, default: int) -> int:
    raw = env_text(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def env_float(name: str, default: float) -> float:
    raw = env_text(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def parse_categories(raw: str) -> list[str]:
    values = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
    if not values:
        raise ValueError("CATS is empty")
    result: list[str] = []
    for value in values:
        if not CATEGORY_PATTERN.fullmatch(value):
            raise ValueError(f"invalid arXiv category: {value!r}")
        if value not in result:
            result.append(value)
    return result


def parse_announce_types(raw: str) -> set[str]:
    allowed = {"new", "cross", "replace", "replace-cross"}
    values = {part.lower() for part in re.split(r"[\s,]+", raw.strip()) if part}
    if not values:
        values = {"new", "cross"}
    unknown = values - allowed
    if unknown:
        raise ValueError(f"invalid ANNOUNCE_TYPES: {sorted(unknown)}")
    return values


def base_id(versioned_id: str) -> str:
    return VERSION_SUFFIX.sub("", versioned_id)


def arxiv_id_from_text(value: str) -> str:
    text = compact_text(value)
    oai_prefix = "oai:arXiv.org:"
    if text.startswith(oai_prefix):
        return text[len(oai_prefix) :]
    for marker in ("/abs/", "/pdf/"):
        if marker in text:
            identifier = text.split(marker, 1)[1].split("?", 1)[0]
            return re.sub(r"\.pdf$", "", identifier)
    return text.rsplit(":", 1)[-1]


def paper_anchor(versioned_id: str) -> str:
    return "paper-" + re.sub(r"[^A-Za-z0-9_.-]", "-", versioned_id)


def category_matches(subscription: str, paper_categories: list[str]) -> bool:
    prefix = subscription + "."
    return any(item == subscription or item.startswith(prefix) for item in paper_categories)


def category_label(category: str) -> str:
    if category in CATEGORY_LABELS:
        return CATEGORY_LABELS[category]
    archive = category.split(".", 1)[0]
    return CATEGORY_LABELS.get(archive, category)


def is_primary_for_subscription(subscription: str, paper: dict[str, Any]) -> bool:
    primary = paper.get("primary_category") or ""
    if not primary and paper.get("categories"):
        primary = paper["categories"][0]
    return bool(primary) and category_matches(subscription, [primary])


def partition_by_primary(
    subscription: str,
    papers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    primary: list[dict[str, Any]] = []
    cross: list[dict[str, Any]] = []
    for paper in papers:
        if is_primary_for_subscription(subscription, paper):
            primary.append(paper)
        else:
            cross.append(paper)
    return primary, cross


def compact_text(value: str) -> str:
    return SPACE_PATTERN.sub(" ", value or "").strip()


def clean_feed_summary(value: str) -> str:
    text = (value or "").strip()
    if text.lower().startswith("arxiv:") and "Abstract:" in text:
        text = text.split("Abstract:", 1)[1]
    return compact_text(text)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def set_github_outputs(**values: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


class ArxivClient:
    def __init__(self, contact: str) -> None:
        self.last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": f"arxiv-digest/2.0 ({contact})"}
        )

    def _wait(self) -> None:
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < ARXIV_MIN_INTERVAL_SECONDS:
            time.sleep(ARXIV_MIN_INTERVAL_SECONDS - elapsed)

    def get(self, url: str, params: dict[str, Any] | None = None) -> str:
        last_error: Exception | None = None
        for attempt in range(5):
            self._wait()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=(15, 75),
                )
                self.last_request_at = time.monotonic()
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"temporary arXiv response: {response.status_code}",
                        response=response,
                    )
                response.raise_for_status()
                return response.text
            except (requests.RequestException, TimeoutError) as exc:
                self.last_request_at = time.monotonic()
                last_error = exc
                if attempt == 4:
                    break
                time.sleep(min(60, 2 ** (attempt + 1)))
        raise RuntimeError("arXiv request failed after retries") from last_error


def parse_feed(xml_text: str, announce_types: set[str]) -> tuple[str, list[dict[str, Any]]]:
    root = ET.fromstring(xml_text)
    feed_updated = compact_text(root.findtext("atom:updated", "", NS))
    papers: dict[str, dict[str, Any]] = {}

    for entry in root.findall("atom:entry", NS):
        id_text = compact_text(entry.findtext("atom:id", "", NS))
        versioned_id = arxiv_id_from_text(id_text)
        if not versioned_id or versioned_id == id_text and "arxiv" not in id_text.lower():
            continue

        announce_type = compact_text(
            entry.findtext("arxiv:announce_type", "new", NS)
        ).lower()
        if announce_type not in announce_types:
            continue

        categories = [
            node.attrib.get("term", "").strip()
            for node in entry.findall("atom:category", NS)
            if node.attrib.get("term", "").strip()
        ]
        authors = [
            compact_text(node.text or "")
            for node in entry.findall("dc:creator", NS)
            if compact_text(node.text or "")
        ]
        if not authors:
            authors = [
                compact_text(node.findtext("atom:name", "", NS))
                for node in entry.findall("atom:author", NS)
                if compact_text(node.findtext("atom:name", "", NS))
            ]

        alternate = ""
        for link in entry.findall("atom:link", NS):
            if link.attrib.get("rel", "alternate") == "alternate":
                alternate = link.attrib.get("href", "")
                break

        papers[versioned_id] = {
            "id": versioned_id,
            "base_id": base_id(versioned_id),
            "title": compact_text(entry.findtext("atom:title", "", NS)),
            "abstract": clean_feed_summary(entry.findtext("atom:summary", "", NS)),
            "authors": authors,
            "categories": list(dict.fromkeys(categories)),
            "primary_category": categories[0] if categories else "",
            "announce_type": announce_type,
            "published": compact_text(entry.findtext("atom:published", "", NS)),
            "url": alternate or f"https://arxiv.org/abs/{versioned_id}",
        }

    return feed_updated, sorted(papers.values(), key=lambda item: item["id"])


def parse_api_response(xml_text: str) -> dict[str, dict[str, Any]]:
    root = ET.fromstring(xml_text)
    result: dict[str, dict[str, Any]] = {}
    for entry in root.findall("atom:entry", NS):
        url_id = arxiv_id_from_text(entry.findtext("atom:id", "", NS))
        identifier = base_id(url_id)
        primary = entry.find("arxiv:primary_category", NS)
        primary_category = primary.attrib.get("term", "") if primary is not None else ""
        categories = [
            node.attrib.get("term", "").strip()
            for node in entry.findall("atom:category", NS)
            if node.attrib.get("term", "").strip()
        ]
        authors = [
            compact_text(node.findtext("atom:name", "", NS))
            for node in entry.findall("atom:author", NS)
            if compact_text(node.findtext("atom:name", "", NS))
        ]
        result[identifier] = {
            "title": compact_text(entry.findtext("atom:title", "", NS)),
            "abstract": compact_text(entry.findtext("atom:summary", "", NS)),
            "authors": authors,
            "categories": list(dict.fromkeys(categories)),
            "primary_category": primary_category,
        }
    return result


def enrich_metadata(client: ArxivClient, papers: list[dict[str, Any]]) -> None:
    identifiers = list(dict.fromkeys(paper["base_id"] for paper in papers))
    metadata: dict[str, dict[str, Any]] = {}
    for start in range(0, len(identifiers), 50):
        chunk = identifiers[start : start + 50]
        xml_text = client.get(
            API_URL,
            params={"id_list": ",".join(chunk), "max_results": len(chunk)},
        )
        metadata.update(parse_api_response(xml_text))

    missing = [identifier for identifier in identifiers if identifier not in metadata]
    if missing:
        raise RuntimeError(f"arXiv API omitted {len(missing)} requested papers")

    for paper in papers:
        extra = metadata[paper["base_id"]]
        for field in ("title", "abstract", "authors"):
            if extra.get(field):
                paper[field] = extra[field]
        paper["categories"] = list(
            dict.fromkeys(paper["categories"] + extra.get("categories", []))
        )
        if extra.get("primary_category"):
            paper["primary_category"] = extra["primary_category"]


def make_batch_id(
    categories: list[str],
    announce_types: set[str],
    papers: list[dict[str, Any]],
) -> str:
    canonical = {
        "categories": sorted(categories),
        "announce_types": sorted(announce_types),
        "papers": sorted(
            f"{paper['id']}:{paper['announce_type']}" for paper in papers
        ),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def translation_cache_key(paper: dict[str, Any], model: str) -> str:
    abstract_hash = hashlib.sha256(paper["abstract"].encode("utf-8")).hexdigest()
    canonical = "|".join(
        [paper["id"], model, PROMPT_VERSION, abstract_hash]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_model_json(value: str) -> dict[str, Any]:
    text = value.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("model response is not a JSON object")
    return parsed


def validate_translation(value: dict[str, Any]) -> dict[str, Any]:
    title = value.get("cn_title")
    abstract = value.get("cn_abstract")
    evaluation = value.get("evaluation")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("cn_title is missing")
    if not isinstance(abstract, str) or not abstract.strip():
        raise ValueError("cn_abstract is missing")
    if not isinstance(evaluation, dict):
        raise ValueError("evaluation is missing")

    checked_evaluation: dict[str, str] = {}
    for field in ("question", "method", "findings", "outlook"):
        item = evaluation.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"evaluation.{field} is missing")
        checked_evaluation[field] = item.strip()

    cjk_title = len(CJK_PATTERN.findall(title))
    cjk_abstract = len(CJK_PATTERN.findall(abstract))
    cjk_evaluation = len(
        CJK_PATTERN.findall(" ".join(checked_evaluation.values()))
    )
    if cjk_title < 2 or cjk_abstract < 20 or cjk_evaluation < 20:
        raise ValueError("model response does not contain enough Chinese text")

    return {
        "cn_title": title.strip(),
        "cn_abstract": abstract.strip(),
        "evaluation": checked_evaluation,
        "flagged": False,
    }


def fallback_translation(paper: dict[str, Any]) -> dict[str, Any]:
    message = "本条目的中文生成失败，暂时保留英文信息，请人工检查。"
    return {
        "cn_title": paper["title"],
        "cn_abstract": paper["abstract"],
        "evaluation": {
            "question": message,
            "method": message,
            "findings": message,
            "outlook": message,
        },
        "flagged": True,
    }


def build_prompt(paper: dict[str, Any]) -> str:
    return f"""
请基于给定论文信息生成严谨、克制的中文学术摘要。不要虚构原文没有的信息。
必须只返回一个 JSON 对象，不要使用 Markdown，不要添加前后说明。

JSON 结构：
{{
  "cn_title": "准确的中文标题",
  "cn_abstract": "忠实、流畅的中文摘要；保留必要的公式和专有名词",
  "evaluation": {{
    "question": "论文研究的问题",
    "method": "采用的方法",
    "findings": "原文明确支持的主要结果",
    "outlook": "局限、适用范围或可合理推断的后续方向；推断必须明确措辞"
  }}
}}

arXiv ID: {paper["id"]}
分类: {", ".join(paper["categories"])}
英文标题: {paper["title"]}
英文摘要:
{paper["abstract"]}
""".strip()


def create_ai_client(api_key: str, base_url: str):
    from openai import OpenAI

    arguments: dict[str, Any] = {
        "api_key": api_key,
        "timeout": 120.0,
        "max_retries": 2,
    }
    if base_url:
        arguments["base_url"] = base_url
    return OpenAI(**arguments)


def request_translation(
    client: Any,
    provider: str,
    model: str,
    paper: dict[str, Any],
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是谨慎的科研编辑。输出必须是有效 JSON，"
                    "不得捏造结论，不得输出 HTML。"
                ),
            },
            {"role": "user", "content": build_prompt(paper)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 2400,
    }
    if provider.lower() == "deepseek":
        arguments["extra_body"] = {"thinking": {"type": "disabled"}}

    response = client.chat.completions.create(**arguments)
    content = response.choices[0].message.content
    if not content:
        raise ValueError("model returned empty content")
    return validate_translation(parse_model_json(content))


def translate_all(
    papers: list[dict[str, Any]],
    cache: dict[str, Any],
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    concurrency: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    items = cache.setdefault("items", {})
    if not isinstance(items, dict):
        raise ValueError("translation cache items must be an object")

    results: dict[str, dict[str, Any]] = {}
    pending: list[tuple[dict[str, Any], str]] = []
    cache_hits = 0

    for paper in papers:
        key = translation_cache_key(paper, model)
        record = items.get(key)
        try:
            if isinstance(record, dict) and isinstance(record.get("result"), dict):
                results[paper["id"]] = validate_translation(record["result"])
                cache_hits += 1
                continue
        except ValueError:
            pass
        pending.append((paper, key))

    if pending:
        if not api_key:
            raise ValueError("AI_API_KEY is empty and uncached papers need processing")
        client = create_ai_client(api_key, base_url)

        def worker(paper: dict[str, Any]) -> dict[str, Any]:
            return request_translation(client, provider, model, paper)

        with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 8))) as executor:
            futures = {
                executor.submit(worker, paper): (paper, key)
                for paper, key in pending
            }
            for future in as_completed(futures):
                paper, key = futures[future]
                try:
                    translated = future.result()
                    results[paper["id"]] = translated
                    items[key] = {
                        "paper_id": paper["id"],
                        "model": model,
                        "prompt_version": PROMPT_VERSION,
                        "created_at": datetime.now(UTC).isoformat(),
                        "result": translated,
                    }
                except Exception as exc:
                    print(
                        f"warning: AI generation failed for {paper['id']}: "
                        f"{type(exc).__name__}",
                        file=sys.stderr,
                    )
                    results[paper["id"]] = fallback_translation(paper)

    flagged = sum(1 for value in results.values() if value.get("flagged"))
    print(
        f"translation summary: total={len(papers)} "
        f"cache_hits={cache_hits} generated={len(pending)} flagged={flagged}"
    )
    return results, flagged


def escape_text(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def render_paragraphs(value: str) -> str:
    blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n", value.strip())
        if block.strip()
    ]
    return "".join(
        f"<p>{escape_text(block).replace(chr(10), '<br>')}</p>"
        for block in blocks
    )


def render_paper(
    paper: dict[str, Any],
    translated: dict[str, Any],
    badge: str = "",
) -> str:
    identifier = escape_text(paper["id"])
    anchor = escape_text(paper_anchor(paper["id"]))
    authors = "、".join(paper["authors"]) if paper["authors"] else "未提供"
    categories = " ".join(
        f"<span class=\"tag\">{escape_text(item)}</span>"
        for item in paper["categories"]
    )
    flag = (
        "<p class=\"warning\">⚠ 中文生成失败，本条目需要人工检查。</p>"
        if translated.get("flagged")
        else ""
    )
    evaluation = translated["evaluation"]
    url = f"https://arxiv.org/abs/{quote(paper['id'], safe='v./')}"
    badge_html = f'<span class="badge">{escape_text(badge)}</span>' if badge else ""
    return f"""
<article class="paper" id="{anchor}">
  <div class="paper-head">
    <h2>{escape_text(translated["cn_title"])}{badge_html}</h2>
    <a href="{escape_text(url)}" rel="noopener noreferrer">{identifier}</a>
  </div>
  <p class="original-title">{escape_text(paper["title"])}</p>
  <p class="meta"><strong>作者：</strong>{escape_text(authors)}</p>
  <p class="meta"><strong>主分类：</strong>{escape_text(paper["primary_category"])}
     <strong>公告类型：</strong>{escape_text(paper["announce_type"])}</p>
  <div class="tags">{categories}</div>
  {flag}
  <section>
    <h3>中文摘要</h3>
    {render_paragraphs(translated["cn_abstract"])}
  </section>
  <section class="evaluation">
    <h3>结构化评述</h3>
    <h4>研究问题</h4>{render_paragraphs(evaluation["question"])}
    <h4>方法</h4>{render_paragraphs(evaluation["method"])}
    <h4>主要发现</h4>{render_paragraphs(evaluation["findings"])}
    <h4>局限与展望</h4>{render_paragraphs(evaluation["outlook"])}
  </section>
</article>
""".strip()


def render_page(
    page_title: str,
    body: str,
    include_mathjax: bool,
) -> str:
    mathjax = ""
    if include_mathjax:
        mathjax = """
  <script>
    window.MathJax = { tex: { inlineMath: [['$', '$'], ['\\\\(', '\\\\)']] } };
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-mml-chtml.js"></script>
"""
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<!doctype html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "  <meta http-equiv=\"Content-Security-Policy\"\n"
        "        content=\"default-src 'self'; script-src 'self' 'unsafe-inline' "
        "https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; "
        "font-src 'self' https://cdn.jsdelivr.net data:; img-src 'self' data:; "
        "object-src 'none'; base-uri 'self'\">\n"
        f"  <title>{escape_text(page_title)}</title>\n"
        "  <style>"
        + PAGE_CSS
        + "</style>"
        + mathjax
        + "\n</head>\n<body>\n"
        + body
        + "  <footer>\n"
        f"    生成时间：{escape_text(generated_at)}。"
        "中文内容由模型辅助生成，请以论文原文为准。\n"
        "  </footer>\n"
        "</body>\n"
        "</html>\n"
    )


def render_home(
    announcement_date: str,
    categories: list[str],
    category_counts: dict[str, int],
    total_unique: int,
) -> str:
    cards: list[str] = []
    for category in categories:
        count = int(category_counts.get(category, 0))
        label = category_label(category)
        if count > 0:
            href = f"{escape_text(category)}-latest.html"
            card = (
                f'<a class="card" href="{href}">'
                f'<div class="card-code">{escape_text(category)}</div>'
                f'<div class="card-name">{escape_text(label)}</div>'
                f'<div class="card-count">{count}<span>篇</span></div>'
                f"</a>"
            )
        else:
            card = (
                f'<div class="card disabled">'
                f'<div class="card-code">{escape_text(category)}</div>'
                f'<div class="card-name">{escape_text(label)}</div>'
                f'<div class="card-count">0<span>篇</span></div>'
                f"</div>"
            )
        cards.append(card)
    body = f"""
  <header>
    <h1>arXiv 每日论文</h1>
    <p class="lede">官方公告日：{escape_text(announcement_date)} · 去重后共 {total_unique} 篇</p>
    <p class="lede">点击分类进入当日论文目录与摘要。</p>
  </header>
  <main class="grid">
    {"".join(cards)}
  </main>
"""
    return render_page("arXiv 每日论文总览", body, include_mathjax=False)


def render_toc_list(
    papers: list[dict[str, Any]],
    translations: dict[str, dict[str, Any]],
) -> str:
    items: list[str] = []
    for paper in papers:
        translated = translations[paper["id"]]
        items.append(
            "<li>"
            f"<a href=\"#{escape_text(paper_anchor(paper['id']))}\">"
            f"{escape_text(translated['cn_title'])}</a>"
            f" <small>{escape_text(paper['id'])}</small>"
            "</li>"
        )
    return f'<ol class="toc-list">{"".join(items)}</ol>'


def render_category_page(
    category: str,
    announcement_date: str,
    primary_papers: list[dict[str, Any]],
    cross_papers: list[dict[str, Any]],
    translations: dict[str, dict[str, Any]],
    subscribed_categories: list[str],
) -> str:
    total = len(primary_papers) + len(cross_papers)
    label = category_label(category)
    navigation = " ".join(
        f"<a href=\"{escape_text(item)}-latest.html\">{escape_text(item)}</a>"
        for item in subscribed_categories
    )
    toc_parts: list[str] = []
    if primary_papers:
        toc_parts.append(
            f"<h3>主分类（{len(primary_papers)}）</h3>"
            + render_toc_list(primary_papers, translations)
        )
    if cross_papers:
        toc_parts.append(
            f"<h3>交叉列表（{len(cross_papers)}）</h3>"
            + render_toc_list(cross_papers, translations)
        )
    toc_html = (
        f'<section class="toc"><h2>目录</h2>{"".join(toc_parts)}</section>'
        if toc_parts
        else '<section class="toc"><h2>目录</h2><p>今日该分类无新论文。</p></section>'
    )
    article_parts: list[str] = []
    if primary_papers:
        article_parts.append(f'<h2 class="section-title">主分类</h2>')
        article_parts.extend(
            render_paper(paper, translations[paper["id"]], "主分类")
            for paper in primary_papers
        )
    if cross_papers:
        article_parts.append(f'<h2 class="section-title">交叉列表</h2>')
        article_parts.extend(
            render_paper(paper, translations[paper["id"]], "交叉列表")
            for paper in cross_papers
        )
    body = f"""
  <header>
    <h1>{escape_text(category)} · {escape_text(label)}</h1>
    <p class="lede">官方公告日：{escape_text(announcement_date)}</p>
    <nav><a href="index.html">总览</a>{navigation}</nav>
    <div class="stats">
      <div class="stat"><div class="stat-label">论文总数</div><div class="stat-value">{total}</div></div>
      <div class="stat"><div class="stat-label">主分类</div><div class="stat-value">{len(primary_papers)}</div></div>
      <div class="stat"><div class="stat-label">交叉列表</div><div class="stat-value">{len(cross_papers)}</div></div>
    </div>
  </header>
  <main>
    {toc_html}
    {"".join(article_parts)}
  </main>
"""
    return render_page(
        f"arXiv {category} 每日论文",
        body,
        include_mathjax=True,
    )


def announcement_date_for(papers: list[dict[str, Any]], feed_updated: str) -> str:
    dates = [
        paper["published"][:10]
        for paper in papers
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", paper["published"][:10])
    ]
    if dates:
        return max(dates)
    if re.match(r"\d{4}-\d{2}-\d{2}", feed_updated):
        return feed_updated[:10]
    return datetime.now(UTC).date().isoformat()


def build_outputs(
    papers: list[dict[str, Any]],
    translations: dict[str, dict[str, Any]],
    categories: list[str],
    announcement_date: str,
    batch_id: str,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    category_counts: dict[str, int] = {}
    memberships: dict[str, list[str]] = {paper["id"]: [] for paper in papers}
    partitioned: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for category in categories:
        selected = [
            paper
            for paper in papers
            if category_matches(category, paper["categories"])
        ]
        primary_papers, cross_papers = partition_by_primary(category, selected)
        partitioned[category] = (primary_papers, cross_papers)
        category_counts[category] = len(selected)
        for paper in selected:
            memberships[paper["id"]].append(category)

    home = render_home(
        announcement_date,
        categories,
        category_counts,
        len(papers),
    )
    write_text(OUTPUT_DIR / "index.html", home)
    write_text(OUTPUT_DIR / "latest.html", home)
    write_text(
        OUTPUT_DIR / "archive" / announcement_date / "index.html",
        home,
    )

    for category in categories:
        primary_papers, cross_papers = partitioned[category]
        if not primary_papers and not cross_papers:
            continue
        category_page = render_category_page(
            category,
            announcement_date,
            primary_papers,
            cross_papers,
            translations,
            categories,
        )
        write_text(OUTPUT_DIR / f"{category}-latest.html", category_page)
        write_text(
            OUTPUT_DIR / "archive" / announcement_date / f"{category}.html",
            category_page,
        )

    notification = {
        "schema_version": 1,
        "batch_id": batch_id,
        "announcement_date": announcement_date,
        "total_unique": len(papers),
        "category_counts": category_counts,
        "items": [
            {
                "id": paper["id"],
                "cn_title": translations[paper["id"]]["cn_title"],
                "categories": memberships[paper["id"]],
                "flagged": bool(translations[paper["id"]].get("flagged")),
            }
            for paper in papers
        ],
    }
    atomic_write_json(RUNTIME_DIR / "notification.json", notification)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the arXiv daily digest")
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild and resend an already processed batch",
    )
    arguments = parser.parse_args()

    categories = parse_categories(env_text("CATS"))
    announce_types = parse_announce_types(
        env_text("ANNOUNCE_TYPES", "new,cross")
    )
    contact = env_text("ARXIV_CONTACT")
    if not contact:
        raise ValueError("ARXIV_CONTACT is required")

    max_papers = env_int("MAX_PAPERS_PER_RUN", 200)
    max_flagged_ratio = env_float("MAX_FLAGGED_RATIO", 0.10)
    ai_concurrency = env_int("AI_CONCURRENCY", 3)
    if max_papers < 1:
        raise ValueError("MAX_PAPERS_PER_RUN must be positive")
    if not 0 <= max_flagged_ratio <= 1:
        raise ValueError("MAX_FLAGGED_RATIO must be between 0 and 1")

    provider = env_text("AI_PROVIDER", "openai-compatible")
    base_url = env_text("AI_BASE_URL")
    model = env_text("AI_MODEL")
    if not model:
        raise ValueError("AI_MODEL is required")

    client = ArxivClient(contact)
    feed_url = FEED_BASE + "+".join(categories)
    feed_xml = client.get(feed_url)
    feed_updated, papers = parse_feed(feed_xml, announce_types)
    papers = [
        paper
        for paper in papers
        if any(
            category_matches(category, paper["categories"])
            for category in categories
        )
    ]

    if not papers:
        print("no matching papers in the current official feed; nothing to send")
        set_github_outputs(skipped="true", batch_id="", reason="no_entries")
        return 0
    if len(papers) > max_papers:
        raise RuntimeError(
            f"batch contains {len(papers)} papers, exceeding "
            f"MAX_PAPERS_PER_RUN={max_papers}"
        )

    batch_id = make_batch_id(categories, announce_types, papers)
    state = load_json(
        STATE_FILE,
        {
            "schema_version": 1,
            "last_batch_id": "",
            "last_announcement_date": "",
            "updated_at": "",
        },
    )
    if state.get("last_batch_id") == batch_id and not arguments.force:
        print(f"batch already processed: {batch_id}")
        set_github_outputs(
            skipped="true",
            batch_id=batch_id,
            reason="already_processed",
        )
        return 0

    enrich_metadata(client, papers)
    cache = load_json(
        CACHE_FILE,
        {"schema_version": 1, "items": {}},
    )
    translations, flagged = translate_all(
        papers=papers,
        cache=cache,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=env_text("AI_API_KEY"),
        concurrency=ai_concurrency,
    )
    atomic_write_json(CACHE_FILE, cache)

    flagged_ratio = flagged / len(papers)
    if flagged_ratio > max_flagged_ratio:
        raise RuntimeError(
            f"flagged translation ratio {flagged_ratio:.1%} exceeds "
            f"MAX_FLAGGED_RATIO={max_flagged_ratio:.1%}"
        )

    announcement_date = announcement_date_for(papers, feed_updated)
    build_outputs(
        papers,
        translations,
        categories,
        announcement_date,
        batch_id,
    )
    atomic_write_json(
        STATE_FILE,
        {
            "schema_version": 1,
            "last_batch_id": batch_id,
            "last_announcement_date": announcement_date,
            "feed_updated": feed_updated,
            "paper_count": len(papers),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    set_github_outputs(
        skipped="false",
        batch_id=batch_id,
        announcement_date=announcement_date,
    )
    print(
        f"built batch={batch_id} date={announcement_date} papers={len(papers)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
