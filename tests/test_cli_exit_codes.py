from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.models.db import create_schema, open_database
from src.models.schema import FeedbackItem
from src.storage.repository import FeedbackRepository

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(db_url: str, *args: str, api_key: str = "") -> subprocess.CompletedProcess:
    """
    Run main.py as a real subprocess.

    OPENAI_API_KEY is passed explicitly (even when empty) because the .env loader
    only fills in variables that are absent from the environment -- an explicit
    empty value therefore suppresses the developer's real key, so this test can
    never reach a live API.
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = db_url
    env["OPENAI_API_KEY"] = api_key
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestCLIExitCodes(unittest.TestCase):
    """
    A pipeline meant to run unattended has to signal failure through its exit
    code. Reporting "failed=1" on stdout while exiting 0 makes a broken run look
    green to cron and CI.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "cli_test.db"
        self.db_url = f"sqlite:///{self.db_path}"

        conn = open_database(self.db_url)
        create_schema(conn)
        FeedbackRepository(conn).save_feedbacks_batch(
            [
                FeedbackItem(
                    id="xhs:aaaaaaaaaaaaaaaaaaaaaaaa",
                    platform="xhs",
                    content="这个软件有个很烦人的bug",
                    author="测试用户",
                    created_at="2026-10-13T00:00:00+00:00",
                    url="https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa",
                    collected_at="2026-10-13T00:00:00+00:00",
                    metrics={"likes": 1},
                )
            ]
        )
        conn.close()

    def tearDown(self):
        self._tmp.cleanup()

    def test_analyze_without_a_key_exits_nonzero(self):
        result = _run_cli(self.db_url, "analyze")
        self.assertEqual(
            result.returncode,
            1,
            f"expected a failure exit code\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        self.assertIn("failed=1", result.stdout)
        self.assertIn("OPENAI_API_KEY is not configured", result.stdout + result.stderr)

    def test_analyze_without_a_key_stores_no_fabricated_result(self):
        _run_cli(self.db_url, "analyze")
        conn = open_database(self.db_url)
        rows = conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0]
        conn.close()
        self.assertEqual(rows, 0, "a missing key must not produce a stored analysis")

    def test_analyze_with_opt_in_heuristic_exits_zero_and_labels_output(self):
        result = _run_cli(self.db_url, "analyze", "--allow-heuristic")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        conn = open_database(self.db_url)
        models = [r[0] for r in conn.execute("SELECT model FROM analysis_results")]
        conn.close()
        self.assertEqual(models, ["heuristic-rule-engine-v1"])

    def test_stats_on_an_empty_database_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'empty.db'}"
            conn = open_database(url)
            create_schema(conn)
            conn.close()
            result = _run_cli(url, "stats")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_trace_of_an_unknown_id_exits_nonzero(self):
        result = _run_cli(self.db_url, "trace", "xhs:doesnotexist")
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
