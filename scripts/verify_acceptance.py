#!/usr/bin/env python3
"""
Acceptance self-check for the feedback pipeline.

Read-only: this script never writes to the database. It inspects the live database
and reports, for each acceptance criterion, whether the evidence is actually present.

Usage:
    python scripts/verify_acceptance.py
    python scripts/verify_acceptance.py --live-urls 10   # also HTTP-check 10 origin links
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models.db import resolve_db_path  # noqa: E402
from src.models.schema import FeedbackItem  # noqa: E402
from src.utils.config import AppConfig  # noqa: E402

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str) -> None:
        self.rows.append((status, name, detail))

    def render(self) -> int:
        width = max(len(name) for _, name, _ in self.rows) + 2
        icon = {PASS: "[OK]  ", FAIL: "[FAIL]", WARN: "[WARN]"}
        print("\n" + "=" * 78)
        print("  验收自检报告 (Acceptance Self-Check)")
        print("=" * 78)
        for status, name, detail in self.rows:
            print(f"{icon[status]} {name:<{width}} {detail}")
        failures = sum(1 for s, _, _ in self.rows if s == FAIL)
        warnings = sum(1 for s, _, _ in self.rows if s == WARN)
        print("=" * 78)
        print(f"  {len(self.rows)} checks: "
              f"{sum(1 for s, _, _ in self.rows if s == PASS)} passed, "
              f"{warnings} warnings, {failures} failed")
        print("=" * 78 + "\n")
        return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-urls", type=int, default=0,
                        help="HTTP-check this many origin URLs (requires network)")
    args = parser.parse_args()

    config = AppConfig.load()
    db_path = resolve_db_path(config.database_url)
    report = Report()

    if not db_path.exists():
        # Expected state on a fresh clone: the database is gitignored, so there is
        # nothing to check until collection has run at least once.
        report.add(
            WARN,
            "数据库存在",
            f"{db_path} 尚未生成 —— 全新克隆的正常状态。"
            "请先运行 `python main.py login`（首次）与 `python main.py run` 采集数据。",
        )
        return report.render()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731

    total = q("SELECT COUNT(*) FROM feedbacks")
    unique_ids = q("SELECT COUNT(DISTINCT id) FROM feedbacks")
    unique_urls = q("SELECT COUNT(DISTINCT url) FROM feedbacks")

    # --- 1. Volume -----------------------------------------------------------
    if total >= 500:
        report.add(PASS, "累计采集 500+ 条真实数据", f"{total} 条")
    elif total > 0:
        report.add(WARN, "累计采集 500+ 条真实数据", f"仅 {total} 条，未达 500")
    else:
        report.add(FAIL, "累计采集 500+ 条真实数据", "数据库为空")

    # --- 2. Idempotency ------------------------------------------------------
    if total == unique_ids:
        report.add(PASS, "幂等：无重复 ID", f"{unique_ids}/{total} 唯一")
    else:
        report.add(FAIL, "幂等：无重复 ID", f"{total - unique_ids} 条重复")

    # --- 3. Traceability -----------------------------------------------------
    if total == unique_urls:
        report.add(PASS, "可追溯：URL 唯一", f"{unique_urls}/{total} 唯一")
    else:
        report.add(FAIL, "可追溯：URL 唯一", f"{total - unique_urls} 条 URL 重复")

    bad_urls = q("SELECT COUNT(*) FROM feedbacks WHERE url NOT LIKE 'http%'")
    if bad_urls == 0:
        report.add(PASS, "可追溯：URL 均为可访问格式", "全部以 http(s):// 开头")
    else:
        report.add(FAIL, "可追溯：URL 均为可访问格式", f"{bad_urls} 条非法 URL")

    # --- 4. Data contract ----------------------------------------------------
    violations = 0
    for row in conn.execute("SELECT * FROM feedbacks"):
        item = FeedbackItem.from_dict(dict(row))
        if item.validate():
            violations += 1
    rate = violations / total if total else 0
    if rate == 0:
        report.add(PASS, "数据契约：全部字段合规", f"{total} 条全部通过校验")
    elif rate <= 0.05:
        report.add(WARN, "数据契约：全部字段合规",
                   f"{violations} 条不合规 ({rate:.1%})，已被质量监控标记")
    else:
        report.add(FAIL, "数据契约：全部字段合规", f"{violations} 条不合规 ({rate:.1%})")

    # --- 5. LLM analysis -----------------------------------------------------
    analyzed = q("SELECT COUNT(*) FROM analysis_results")
    if total and analyzed == total:
        report.add(PASS, "LLM 分析覆盖", f"{analyzed}/{total} 条已分析")
    elif analyzed:
        report.add(WARN, "LLM 分析覆盖", f"{analyzed}/{total} 条已分析，仍有待处理")
    else:
        report.add(WARN, "LLM 分析覆盖", "尚未运行 main.py analyze")

    models = {r["model"]: r["n"] for r in conn.execute(
        "SELECT model, COUNT(*) n FROM analysis_results GROUP BY model")}
    if models:
        real = {m: n for m, n in models.items() if m != "heuristic-rule-engine-v1"}
        if real and "heuristic-rule-engine-v1" not in models:
            report.add(PASS, "LLM 分析来自真实模型", json.dumps(models, ensure_ascii=False))
        elif real:
            report.add(WARN, "LLM 分析来自真实模型",
                       f"混有规则引擎输出: {json.dumps(models, ensure_ascii=False)}")
        else:
            report.add(WARN, "LLM 分析来自真实模型",
                       "全部由规则引擎产生，非真实 LLM")

    orphan = q("""SELECT COUNT(*) FROM analysis_results a
                  LEFT JOIN feedbacks f ON a.feedback_id = f.id
                  WHERE f.id IS NULL""")
    if orphan == 0:
        report.add(PASS, "分析结果均可关联到原帖", "无悬空外键")
    else:
        report.add(FAIL, "分析结果均可关联到原帖", f"{orphan} 条分析找不到对应原帖")

    # --- 6. Incremental state -------------------------------------------------
    targets = q("SELECT COUNT(*) FROM checkpoints")
    if targets:
        with_seen = q("SELECT COUNT(*) FROM checkpoints WHERE seen_ids != '[]' AND seen_ids IS NOT NULL")
        report.add(PASS, "增量水位线已持久化", f"{targets} 个目标，其中 {with_seen} 个有已见 ID 集合")
    else:
        report.add(WARN, "增量水位线已持久化", "无断点记录，可能尚未采集")

    # --- 7. Telemetry --------------------------------------------------------
    runs = q("SELECT COUNT(*) FROM collection_runs")
    failed = q("SELECT COUNT(*) FROM collection_runs WHERE status='failed'")
    if runs:
        report.add(PASS, "质量监控：运行遥测已记录",
                   f"{runs} 次运行，{failed} 次失败（失败会被明确记录而非静默）")
    else:
        report.add(WARN, "质量监控：运行遥测已记录", "无运行记录")

    # --- 8. Optional live URL check ------------------------------------------
    if args.live_urls > 0:
        import httpx
        sample = [r["url"] for r in conn.execute(
            "SELECT url FROM feedbacks ORDER BY RANDOM() LIMIT ?", (args.live_urls,))]
        reachable = 0
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as client:
            for url in sample:
                try:
                    if client.get(url).status_code < 400:
                        reachable += 1
                except Exception:
                    pass
        status = PASS if reachable == len(sample) else WARN
        report.add(status, f"抽样 {args.live_urls} 条链接实际可达",
                   f"{reachable}/{len(sample)} 返回 <400")

    conn.close()
    return report.render()


if __name__ == "__main__":
    raise SystemExit(main())
