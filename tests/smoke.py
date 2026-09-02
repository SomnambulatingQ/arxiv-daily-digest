from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


digest = load_module("digest_run", ROOT / "scripts" / "run.py")
notify = load_module("digest_notify", ROOT / "scripts" / "notify.py")


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
  <updated>2026-08-31T00:00:00-04:00</updated>
  <entry>
    <id>oai:arXiv.org:2608.12345v1</id>
    <published>2026-08-31T00:00:00-04:00</published>
    <title>Danger &lt;script&gt;alert(1)&lt;/script&gt;</title>
    <summary>arXiv:2608.12345 Announce Type: new Abstract: We test a safe renderer.</summary>
    <arxiv:announce_type>new</arxiv:announce_type>
    <category term="hep-th"/>
    <category term="gr-qc"/>
    <dc:creator>Alice Example</dc:creator>
    <link rel="alternate" href="https://arxiv.org/abs/2608.12345v1"/>
  </entry>
  <entry>
    <id>oai:arXiv.org:2608.54321v2</id>
    <published>2026-08-31T00:00:00-04:00</published>
    <title>Replacement excluded by default</title>
    <summary>arXiv:2608.54321 Announce Type: replace Abstract: Updated text.</summary>
    <arxiv:announce_type>replace</arxiv:announce_type>
    <category term="hep-th"/>
    <dc:creator>Bob Example</dc:creator>
  </entry>
</feed>
"""


def translated_result() -> dict:
    return {
        "cn_title": "安全的中文标题 <b>不是标签</b>",
        "cn_abstract": "这是一段足够长的中文摘要，用于检查结构校验、页面渲染以及字符转义是否能够正常工作。",
        "evaluation": {
            "question": "本文研究如何可靠地测试自动生成的论文日报页面。",
            "method": "使用固定输入进行离线解析并检查关键输出。",
            "findings": "测试确认批次计算稳定且危险字符已经被转义。",
            "outlook": "后续仍需在真实账号环境完成端到端验收。",
        },
        "flagged": False,
    }


def main() -> None:
    assert digest.parse_categories("hep-th, gr-qc hep-th") == ["hep-th", "gr-qc"]
    assert digest.parse_announce_types("") == {"new", "cross"}
    assert digest.category_matches("physics", ["physics.comp-ph"])
    assert not digest.category_matches("hep-th", ["hep-ph"])
    assert (
        digest.arxiv_id_from_text(
            "https://export.arxiv.org/abs/hep-th/9901001v2"
        )
        == "hep-th/9901001v2"
    )

    updated, papers = digest.parse_feed(FEED, {"new", "cross"})
    assert updated.startswith("2026-08-31")
    assert len(papers) == 1
    assert papers[0]["id"] == "2608.12345v1"
    assert papers[0]["abstract"] == "We test a safe renderer."

    batch_a = digest.make_batch_id(["hep-th", "gr-qc"], {"new", "cross"}, papers)
    batch_b = digest.make_batch_id(["gr-qc", "hep-th"], {"cross", "new"}, papers)
    assert batch_a == batch_b

    checked = digest.validate_translation(translated_result())
    assert not checked["flagged"]

    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        digest.OUTPUT_DIR = temporary_root / "outputs-public"
        digest.RUNTIME_DIR = temporary_root / "runtime"
        digest.OUTPUT_DIR.mkdir(parents=True)
        preserved = digest.OUTPUT_DIR / "hep-ph-latest.html"
        preserved.write_text("previous digest", encoding="utf-8")
        digest.build_outputs(
            papers,
            {papers[0]["id"]: checked},
            ["hep-th", "gr-qc", "hep-ph"],
            "2026-08-31",
            batch_a,
        )

        latest = (digest.OUTPUT_DIR / "latest.html").read_text(encoding="utf-8")
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in latest
        assert "&lt;b&gt;不是标签&lt;/b&gt;" in latest
        assert (digest.OUTPUT_DIR / "hep-th-latest.html").exists()
        assert (digest.OUTPUT_DIR / "gr-qc-latest.html").exists()
        assert preserved.read_text(encoding="utf-8") == "previous digest"

        notification_path = digest.RUNTIME_DIR / "notification.json"
        notification = json.loads(notification_path.read_text(encoding="utf-8"))
        assert notification["total_unique"] == 1
        content = notify.build_content(
            notification,
            "https://example.github.io/arxiv/",
            20,
        )
        assert "https://example.github.io/arxiv/latest.html" in content
        assert "https://example.github.io/arxiv/hep-th-latest.html" in content
        assert "https://example.github.io/arxiv/gr-qc-latest.html" in content
        assert "hep-ph-latest.html" not in content
        assert "&lt;b&gt;不是标签&lt;/b&gt;" in content

        state_path = temporary_root / "state.json"
        digest.atomic_write_json(state_path, {"ok": True})
        assert json.loads(state_path.read_text(encoding="utf-8")) == {"ok": True}

    print("offline smoke test passed")


if __name__ == "__main__":
    main()
