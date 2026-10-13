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


async def cmd_run_async(args):
    pipeline = FeedbackPipeline()
    platforms = [args.platform] if hasattr(args, "platform") and args.platform else None
    try:
        await pipeline.execute_full_pipeline(platforms=platforms)
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

    # collect
    collect_parser = subparsers.add_parser("collect", help="Run data collection only")
    collect_parser.add_argument("--platform", choices=["discord", "xhs"], help="Limit to specific platform")

    # analyze
    analyze_parser = subparsers.add_parser("analyze", help="Run LLM analysis on pending data")
    analyze_parser.add_argument("--batch-size", type=int, default=50, help="Batch size for LLM analysis")

    # stats
    subparsers.add_parser("stats", help="Show database metrics and statistics")

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
        pipeline = FeedbackPipeline()
        try:
            pipeline.run_analysis(batch_size=args.batch_size)
        finally:
            pipeline.close()
    elif args.subcommand == "stats":
        cmd_stats()
    elif args.subcommand == "trace":
        cmd_trace(args.feedback_id)
    elif args.subcommand == "test":
        cmd_test()


if __name__ == "__main__":
    main()
