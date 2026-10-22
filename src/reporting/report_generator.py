"""Self-contained HTML report generator.

Reads aggregated statistics and representative samples from the SQLite
database and renders a single-file HTML report (pure CSS charts, no JS or
external assets) that can be opened directly in any browser -- the goal is
that a non-technical reader (HR, product manager) can open the file and
immediately understand what users are saying about the product.
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SENTIMENT_LABELS = {"positive": "好评", "negative": "差评", "neutral": "中性"}
SENTIMENT_COLORS = {"positive": "#2e9e5b", "negative": "#d64545", "neutral": "#8a8f98"}
CATEGORY_LABELS = {
    "bug": "Bug 报告",
    "feature_request": "功能建议",
    "inquiry": "使用咨询",
    "praise": "点赞好评",
    "other": "其他",
}


class ReportGenerator:
    """Generate a self-contained HTML report from the pipeline database."""

    def __init__(self, conn: sqlite3.Connection, output_dir: str = "reports"):
        self.conn = conn
        self.output_dir = Path(output_dir)

    # ------------------------------------------------------------------
    # Data gathering
    # ------------------------------------------------------------------
    def _fetch_one(self, sql: str) -> Any:
        cur = self.conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else 0

    def _fetch_all(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def gather(self) -> Dict[str, Any]:
        total = self._fetch_one("SELECT COUNT(*) FROM feedbacks")
        analyzed = self._fetch_one("SELECT COUNT(*) FROM analysis_results")
        authors = self._fetch_one("SELECT COUNT(DISTINCT author) FROM feedbacks")
        sentiment = {
            r["sentiment"]: r["cnt"]
            for r in self._fetch_all(
                "SELECT sentiment, COUNT(*) AS cnt FROM analysis_results GROUP BY sentiment"
            )
        }
        category = {
            r["category"]: r["cnt"]
            for r in self._fetch_all(
                "SELECT category, COUNT(*) AS cnt FROM analysis_results GROUP BY category"
            )
        }
        trend = self._fetch_all(
            """SELECT substr(f.created_at, 1, 7) AS month,
                      ar.sentiment AS sentiment,
                      COUNT(*) AS cnt
               FROM feedbacks f
               JOIN analysis_results ar ON ar.feedback_id = f.id
               GROUP BY month, sentiment
               ORDER BY month"""
        )
        runs = self._fetch_all(
            "SELECT status, COUNT(*) AS cnt FROM collection_runs GROUP BY status"
        )
        # Representative samples: top-liked items per category (fallback: newest).
        samples: List[Dict[str, Any]] = []
        for cat in category:
            try:
                rows = self._fetch_all(
                    """SELECT f.content, f.author, f.created_at, f.url, f.metrics,
                              ar.sentiment, ar.summary
                       FROM feedbacks f
                       JOIN analysis_results ar ON ar.feedback_id = f.id
                       WHERE ar.category = ?
                       ORDER BY CAST(COALESCE(json_extract(f.metrics, '$.likes'), 0) AS INTEGER) DESC
                       LIMIT 3""",
                    (cat,),
                )
            except sqlite3.OperationalError:
                # Older SQLite builds without json_extract: fall back to newest-first.
                rows = self._fetch_all(
                    """SELECT f.content, f.author, f.created_at, f.url, f.metrics,
                              ar.sentiment, ar.summary
                       FROM feedbacks f
                       JOIN analysis_results ar ON ar.feedback_id = f.id
                       WHERE ar.category = ?
                       ORDER BY f.created_at DESC
                       LIMIT 3""",
                    (cat,),
                )
            for row in rows:
                row["category"] = cat
                samples.append(row)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total": total,
            "analyzed": analyzed,
            "authors": authors,
            "sentiment": sentiment,
            "category": category,
            "trend": trend,
            "runs": runs,
            "samples": samples,
        }

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _bar_chart(title: str, data: Dict[str, int], labels: Dict[str, str],
                   colors: Optional[Dict[str, str]] = None) -> str:
        total = sum(data.values()) or 1
        rows = []
        for key, cnt in sorted(data.items(), key=lambda kv: -kv[1]):
            pct = cnt / total * 100
            color = (colors or {}).get(key, "#4a7fd4")
            label = labels.get(key, key)
            rows.append(
                f"<div class='bar-row'><span class='bar-label'>{html.escape(label)}</span>"
                f"<div class='bar-track'><div class='bar-fill' style='width:{pct:.1f}%;background:{color}'></div></div>"
                f"<span class='bar-value'>{cnt} ({pct:.1f}%)</span></div>"
            )
        return (
            f"<div class='card'><h3>{html.escape(title)}</h3>"
            + "".join(rows) + "</div>"
        )

    def _trend_chart(self, trend: List[Dict[str, Any]]) -> str:
        months = sorted({r["month"] for r in trend})
        by_month: Dict[str, Dict[str, int]] = {m: {} for m in months}
        for r in trend:
            by_month[r["month"]][r["sentiment"]] = r["cnt"]
        max_cnt = max([r["cnt"] for r in trend], default=0) or 1
        rows = []
        for m in months:
            cells = []
            for s in ("positive", "neutral", "negative"):
                cnt = by_month[m].get(s, 0)
                h = cnt / max_cnt * 100
                color = SENTIMENT_COLORS.get(s, "#8a8f98")
                cells.append(
                    f"<div class='trend-cell' title='{s}: {cnt}' style='height:{max(h, 3):.0f}%;background:{color}'></div>"
                )
            rows.append(
                f"<div class='trend-col'><div class='trend-stack'>{''.join(cells)}</div>"
                f"<span class='trend-month'>{m}</span></div>"
            )
        legend = "".join(
            f"<span class='legend-item'><i style='background:{c}'></i>{SENTIMENT_LABELS.get(s, s)}</span>"
            for s, c in SENTIMENT_COLORS.items()
        )
        return (
            "<div class='card'><h3>月度反馈趋势</h3>"
            f"<div class='trend-chart'>{''.join(rows)}</div>"
            f"<div class='legend'>{legend}</div></div>"
        )

    def _samples_section(self, samples: List[Dict[str, Any]]) -> str:
        if not samples:
            return "<div class='card'><h3>代表性反馈</h3><p>暂无数据。</p></div>"
        blocks = []
        for s in samples:
            try:
                metrics = json.loads(s["metrics"]) if isinstance(s["metrics"], str) else (s["metrics"] or {})
            except Exception:
                metrics = {}
            likes = metrics.get("likes", "-")
            cat = CATEGORY_LABELS.get(s["category"], s["category"])
            sent = SENTIMENT_LABELS.get(s["sentiment"], s["sentiment"] or "")
            scolor = SENTIMENT_COLORS.get(s["sentiment"], "#8a8f98")
            content = html.escape((s["content"] or "")[:120])
            summary = html.escape(s["summary"] or "")
            blocks.append(
                f"<div class='sample'><div class='sample-head'>"
                f"<span class='tag' style='background:#eef2f8'>{cat}</span>"
                f"<span class='tag' style='background:{scolor};color:#fff'>{sent}</span>"
                f"<span class='likes'>👍 {likes}</span></div>"
                f"<p class='sample-content'>{content}</p>"
                f"<p class='sample-summary'>AI 结论：{summary}</p></div>"
            )
        return "<div class='card'><h3>代表性反馈（按分类取互动量最高）</h3>" + "".join(blocks) + "</div>"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def render_html(self, data: Optional[Dict[str, Any]] = None) -> str:
        d = data or self.gather()
        neg = d["sentiment"].get("negative", 0)
        pos = d["sentiment"].get("positive", 0)
        tone = "整体口碑偏正面" if pos >= neg else "负面反馈占比较高，需要关注"
        kpis = "".join(
            f"<div class='kpi'><div class='kpi-num'>{v}</div><div class='kpi-label'>{k}</div></div>"
            for k, v in [
                ("累计真实反馈", d["total"]),
                ("不同用户数", d["authors"]),
                ("AI 已分析", d["analyzed"]),
                ("差评数", neg),
            ]
        )
        runs = " · ".join(f"{r['status']}: {r['cnt']} 次" for r in d["runs"]) or "暂无运行记录"
        css = """
        :root{--ink:#1f2329;--muted:#6b7280;--line:#e5e7eb;--bg:#f6f7f9}
        *{box-sizing:border-box;margin:0;padding:0}
        body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--ink);padding:32px 16px}
        .wrap{max-width:960px;margin:0 auto}
        h1{font-size:26px;margin-bottom:4px}
        .sub{color:var(--muted);font-size:13px;margin-bottom:24px}
        .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
        .kpi{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;text-align:center}
        .kpi-num{font-size:26px;font-weight:700}
        .kpi-label{font-size:12px;color:var(--muted);margin-top:4px}
        .card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
        .card h3{font-size:15px;margin-bottom:14px}
        .bar-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
        .bar-label{width:80px;font-size:13px;text-align:right;flex-shrink:0}
        .bar-track{flex:1;height:18px;background:#f1f3f5;border-radius:4px;overflow:hidden}
        .bar-fill{height:100%;border-radius:4px}
        .bar-value{width:110px;font-size:12px;color:var(--muted)}
        .trend-chart{display:flex;align-items:flex-end;gap:6px;height:160px;padding:8px 0}
        .trend-col{flex:1;display:flex;flex-direction:column;align-items:center;height:100%}
        .trend-stack{flex:1;display:flex;flex-direction:column-reverse;gap:2px;width:100%;justify-content:flex-start}
        .trend-cell{width:100%;border-radius:2px;min-height:2px}
        .trend-month{font-size:10px;color:var(--muted);margin-top:6px;transform:rotate(-45deg)}
        .legend{display:flex;gap:16px;margin-top:12px;font-size:12px;color:var(--muted)}
        .legend-item{display:flex;align-items:center;gap:4px}
        .legend-item i{width:10px;height:10px;border-radius:2px;display:inline-block}
        .sample{border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:10px}
        .sample-head{display:flex;align-items:center;gap:8px;margin-bottom:8px}
        .tag{font-size:11px;padding:2px 8px;border-radius:10px}
        .likes{font-size:12px;color:var(--muted);margin-left:auto}
        .sample-content{font-size:13px;margin-bottom:6px}
        .sample-summary{font-size:12px;color:var(--muted)}
        .foot{font-size:12px;color:var(--muted);text-align:center;margin-top:8px}
        """
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>用户反馈分析报告</title>
<style>{css}</style></head>
<body><div class="wrap">
<h1>用户反馈分析报告</h1>
<div class="sub">生成时间：{d['generated_at']} · 数据来源：多平台真实采集 · {tone}</div>
<div class="kpis">{kpis}</div>
{self._bar_chart('情感分布（用户是在夸还是在骂？）', d['sentiment'], SENTIMENT_LABELS, SENTIMENT_COLORS)}
{self._bar_chart('问题分类（用户在反馈什么？）', d['category'], CATEGORY_LABELS)}
{self._trend_chart(d['trend'])}
{self._samples_section(d['samples'])}
<div class='card'><h3>采集运行健康度</h3><p style='font-size:13px'>{html.escape(runs)}</p></div>
<div class="foot">由 feedback-pipeline 自动生成 · 每条结论均可通过 trace 命令溯源到原始帖子</div>
</div></body></html>"""

    def save_html(self, path: Optional[str] = None) -> str:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out = Path(path) if path else self.output_dir / "feedback_report.html"
        out.write_text(self.render_html(), encoding="utf-8")
        return str(out)
