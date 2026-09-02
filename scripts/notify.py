from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


PUSHPLUS_URL = "https://www.pushplus.plus/send"


def env_text(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def load_notification(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("notification file has an invalid structure")
    return value


def paper_anchor(versioned_id: str) -> str:
    return "paper-" + re.sub(r"[^A-Za-z0-9_.-]", "-", versioned_id)


def build_content(
    notification: dict[str, Any],
    deployment_url: str,
    max_items: int,
) -> str:
    root_url = deployment_url.rstrip("/") + "/"
    latest_url = urljoin(root_url, "latest.html")
    counts = notification.get("category_counts", {})
    count_text = "；".join(
        f"{html.escape(str(category))}: {int(count)}"
        for category, count in counts.items()
    )
    category_links = " ".join(
        (
            f"<a href=\"{html.escape(urljoin(root_url, f'{category}-latest.html'), quote=True)}\">"
            f"{html.escape(str(category))}（{int(count)}）</a>"
        )
        for category, count in counts.items()
        if int(count) > 0
    )

    rows: list[str] = []
    for item in notification["items"][:max_items]:
        identifier = str(item.get("id", ""))
        title = html.escape(str(item.get("cn_title", identifier)))
        anchor = paper_anchor(identifier)
        link = html.escape(latest_url + "#" + anchor, quote=True)
        categories = " / ".join(str(value) for value in item.get("categories", []))
        warning = " ⚠" if item.get("flagged") else ""
        rows.append(
            "<li>"
            f"<a href=\"{link}\">{title}</a>{warning}"
            f"<br><small>{html.escape(identifier)} · "
            f"{html.escape(categories)}</small>"
            "</li>"
        )

    omitted = len(notification["items"]) - len(rows)
    more = f"<p>另有 {omitted} 篇，请在网页查看。</p>" if omitted > 0 else ""
    return (
        f"<p>官方公告日：{html.escape(str(notification.get('announcement_date', '')))}</p>"
        f"<p>去重后共 {int(notification.get('total_unique', len(notification['items'])))} 篇。"
        f"{count_text}</p>"
        f"<p>分类页面：{category_links}</p>"
        f"<ol>{''.join(rows)}</ol>"
        f"{more}"
        f"<p><a href=\"{html.escape(latest_url, quote=True)}\">打开完整日报</a></p>"
    )


def send_pushplus(payload: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                PUSHPLUS_URL,
                json=payload,
                timeout=(15, 45),
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(
                    f"temporary PushPlus response: {response.status_code}",
                    response=response,
                )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("PushPlus returned a non-object JSON response")
            if str(result.get("code")) != "200":
                raise RuntimeError(
                    f"PushPlus rejected the message: code={result.get('code')}, "
                    f"msg={result.get('msg', '')}"
                )
            return result
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt == 2:
                break
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("PushPlus notification failed after retries") from last_error


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: notify.py PATH_TO_NOTIFICATION_JSON")

    token = env_text("PUSHPLUS_TOKEN")
    deployment_url = env_text("DEPLOYMENT_URL")
    if not token:
        raise ValueError("PUSHPLUS_TOKEN is empty")
    if not deployment_url.startswith(("https://", "http://")):
        raise ValueError("DEPLOYMENT_URL must be an HTTP(S) URL")

    try:
        max_items = int(env_text("PUSH_MAX_ITEMS", "20"))
    except ValueError as exc:
        raise ValueError("PUSH_MAX_ITEMS must be an integer") from exc
    if max_items < 1:
        raise ValueError("PUSH_MAX_ITEMS must be positive")

    notification = load_notification(Path(sys.argv[1]))
    payload: dict[str, Any] = {
        "token": token,
        "title": (
            f"arXiv 日报 {notification.get('announcement_date', '')} "
            f"({notification.get('total_unique', len(notification['items']))} 篇)"
        ),
        "content": build_content(notification, deployment_url, max_items),
        "template": "html",
    }
    topic = env_text("PUSHPLUS_TOPIC")
    if topic:
        payload["topic"] = topic

    result = send_pushplus(payload)
    print(
        "PushPlus accepted the notification: "
        f"code={result.get('code')} msg={result.get('msg', '')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
