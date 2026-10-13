#!/usr/bin/env python3
"""
Main entry point for Multi-Platform User Feedback Collection & Analysis Pipeline.
Usage:
    python main.py run                     # Run full pipeline (collect -> analyze -> report)
    python main.py collect --platform xhs  # Run collector for specific platform
    python main.py analyze                 # Run LLM analysis on unanalyzed records
    python main.py stats                   # View database statistics and quality telemetry
    python main.py trace <feedback_id>     # Trace analysis back to original source URL
    python main.py test                    # Run test suite
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).parent))

from src.models.db import get_db_connection, init_db
from src.pipeline import FeedbackPipeline
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig


def cmd_stats():
    init_db()
    conn = get_db_connection()
    repo = FeedbackRepository(conn)
    summary = repo.get_summary_statistics()
    conn.close()

    print("\n" + "=" * 55)
    print("       USER FEEDBACK PIPELINE METRICS & STATS       ")
    print("=" * 55)
    print(f"Total Feedback Items Stored: {summary['total_feedbacks']}")
    print(f"Platform Breakdown:          {json.dumps(summary['platform_breakdown'], ensure_ascii=False, indent=2)}")
    print(f"Total Analyzed Items:        {summary['total_analyzed']}")
    print(f"Pending Unanalyzed Items:    {summary['unanalyzed_feedbacks']}")
    print(f"Sentiment Breakdown:         {json.dumps(summary['sentiment_distribution'], ensure_ascii=False, indent=2)}")
    print(f"Category Breakdown:          {json.dumps(summary['category_distribution'], ensure_ascii=False, indent=2)}")
    print(f"Total Collection Runs:       {summary['total_collection_runs']}")
    print("=" * 55 + "\n")


def cmd_trace(feedback_id: str):
    init_db()
    conn = get_db_connection()
    repo = FeedbackRepository(conn)
    item = repo.get_traceable_feedback(feedback_id)
    conn.close()

    if not item:
        print(f"\n[ERROR] Feedback with ID '{feedback_id}' not found in database.\n")
        sys.exit(1)

    print("\n" + "=" * 65)
    print("               ORIGIN POST TRACEABILITY REPORT              ")
    print("=" * 65)
    print(f"Feedback ID:      {item['id']}")
    print(f"Platform:         {item['platform']}")
    print(f"Author:           {item['author']}")
    print(f"Created At:       {item['created_at']}")
    print(f"ORIGIN POST URL:  {item['url']}")
    print(f"Content:          {item['content']}")
    print(f"Metrics:          {item.get('metrics', {})}")
    print("-" * 65)
    print("LLM ANALYSIS CONCLUSION:")
    print(f"Sentiment:        {item.get('sentiment')}")
    print(f"Category:         {item.get('category')}")
    print(f"Summary:          {item.get('summary')}")
    print(f"Confidence:       {item.get('confidence')}")
    print(f"Model:            {item.get('model')}")
    print("=" * 65 + "\n")


def cmd_test():
    import unittest
    loader = unittest.TestLoader()
    tests_dir = Path(__file__).parent / "tests"
    suite = loader.discover(start_dir=str(tests_dir), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


def cmd_login():
    """
    One-time interactive Xiaohongshu login. Opens a visible browser, waits for the
    operator to scan the QR code, then persists the session for later reuse.
    """
    from src.collectors.xhs_collector import XHSCollector
    from src.errors import PipelineError

    config = AppConfig.load()
    pipeline = FeedbackPipeline(config)
    try:
        collector = pipeline.collectors["xhs"]
        print("\n" + "=" * 62)
        print("  小红书登录 (仅需一次)")
        print("=" * 62)
        print("  1. 即将弹出一个 Chrome 窗口")
        print("  2. 用小红书 App 扫码登录")
        print("  3. 登录成功后脚本会自动保存会话并退出")
        print("=" * 62 + "\n")
        path = asyncio.run(collector.interactive_login())
        print(f"\n[OK] 登录态已保存到: {path}")
        print("     后续采集会自动复用，无需重复登录。\n")
    except PipelineError as exc:
        print(f"\n[FAILED] {exc}\n")
        sys.exit(1)
    finally:
        pipeline.close()


def cmd_inspect(keyword: str = None):
    """Dump live Xiaohongshu search-page diagnostics so selectors can be re-tuned."""
    from src.collectors.xhs_collector import XHSCollector

    config = AppConfig.load()
    pipeline = FeedbackPipeline(config)
    try:
        collector = pipeline.collectors["xhs"]
        keyword = keyword or (collector.get_targets() or ["AI工具"])[0]
        print(f"\n[inspect] Probing keyword: {keyword!r}")
        print(f"[inspect] Saved session present: {collector.has_saved_session()}")
        report = asyncio.run(collector.dump_dom_diagnostics(keyword))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        pipeline.close()


async def cmd_run_async(args):
    pipeline = FeedbackPipeline()
    platforms = [args.platform] if hasattr(args, "platform") and args.platform else None
    try:
        await pipeline.execute_full_pipeline(
            platforms=platforms,
            batch_size=getattr(args, "batch_size", 100),
            allow_heuristic=getattr(args, "allow_heuristic", False),
        )
    finally:
        pipeline.close()


async def cmd_analyze_async(args):
    pipeline = FeedbackPipeline()
    try:
        stats = await pipeline.run_analysis(
            batch_size=args.batch_size, allow_heuristic=args.allow_heuristic
        )
        print(
            f"\n[analyze] analyzed={stats['analyzed']} "
            f"failed={stats['failed']} skipped={stats['skipped']}\n"
        )
    finally:
        pipeline.close()


async def cmd_collect_async(args):
    pipeline = FeedbackPipeline()
    platforms = [args.platform] if args.platform else None
    try:
        await pipeline.run_collectors(platforms=platforms)
    finally:
        pipeline.close()


def main():
    parser = argparse.ArgumentParser(description="Multi-Platform User Feedback Pipeline")
    subparsers = parser.add_subparsers(dest="subcommand", help="Sub-commands")

    # run
    run_parser = subparsers.add_parser("run", help="Run full pipeline (collection + analysis)")
    run_parser.add_argument("--platform", choices=["discord", "xhs"], help="Limit to specific platform")
    run_parser.add_argument("--batch-size", type=int, default=100, help="Batch size for LLM analysis")
    run_parser.add_argument(
        "--allow-heuristic",
        action="store_true",
        help="Permit the offline rule engine when no LLM key is set (results are labelled as such)",
    )

    # collect
    collect_parser = subparsers.add_parser("collect", help="Run data collection only")
    collect_parser.add_argument("--platform", choices=["discord", "xhs"], help="Limit to specific platform")

    # analyze
    analyze_parser = subparsers.add_parser("analyze", help="Run LLM analysis on pending data")
    analyze_parser.add_argument("--batch-size", type=int, default=50, help="Batch size for LLM analysis")
    analyze_parser.add_argument(
        "--allow-heuristic",
        action="store_true",
        help="Permit the offline rule engine when no LLM key is set (results are labelled as such)",
    )

    # stats
    subparsers.add_parser("stats", help="Show database metrics and statistics")

    # login (Xiaohongshu session bootstrap)
    subparsers.add_parser(
        "login", help="Open a browser to log into Xiaohongshu once and persist the session"
    )

    # inspect (DOM diagnostics)
    inspect_parser = subparsers.add_parser(
        "inspect", help="Dump live page diagnostics to re-tune selectors"
    )
    inspect_parser.add_argument("keyword", nargs="?", default=None, help="Search keyword to probe")

    # trace
    trace_parser = subparsers.add_parser("trace", help="Trace an item and analysis back to source URL")
    trace_parser.add_argument("feedback_id", help="The feedback ID to audit")

    # test
    subparsers.add_parser("test", help="Run test suite")

    args = parser.parse_args()

    if not args.subcommand or args.subcommand == "run":
        asyncio.run(cmd_run_async(args))
    elif args.subcommand == "collect":
        asyncio.run(cmd_collect_async(args))
    elif args.subcommand == "analyze":
        asyncio.run(cmd_analyze_async(args))
    elif args.subcommand == "login":
        cmd_login()
    elif args.subcommand == "inspect":
        cmd_inspect(args.keyword)
    elif args.subcommand == "stats":
        cmd_stats()
    elif args.subcommand == "trace":
        cmd_trace(args.feedback_id)
    elif args.subcommand == "test":
        cmd_test()


if __name__ == "__main__":
    main()
