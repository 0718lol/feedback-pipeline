from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from src.models.schema import AnalysisResult, FeedbackItem, current_iso_time
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig
from src.utils.logger import setup_logger
from src.utils.retry import retry_with_backoff


SYSTEM_PROMPT = """You are an expert AI Data Engineering assistant analyzing user feedback.
Analyze the following user feedback item and extract structured insights.

You must respond ONLY with a valid JSON object strictly matching this schema:
{
  "sentiment": "positive" | "negative" | "neutral",
  "category": "bug" | "feature_request" | "inquiry" | "praise" | "other",
  "summary": "A concise one-sentence summary of the user's core feedback point",
  "confidence": 0.0 to 1.0 (float)
}

Do not include any introductory remarks, explanations, or extra commentary outside the JSON object.
"""


class LLMAnalyzer:
    """
    Analyzes raw feedback content using OpenAI-compatible LLM API.
    Guarantees:
    - Structured JSON extraction
    - Strict validation against AnalysisResult contract
    - Automatic linkage to feedback_id for end-to-end traceability
    - Offline heuristic fallback when API key is unset
    """

    def __init__(self, config: AppConfig, repo: FeedbackRepository):
        self.config = config
        self.repo = repo
        self.logger = setup_logger("analyzer.llm")

    def _clean_json_response(self, text: str) -> str:
        """Strip markdown fences (```json ... ```) or extra prose."""
        text = text.strip()
        # Remove markdown fences
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if fence_match:
            text = fence_match.group(1).strip()
        # Find outer braces if surrounded by text
        brace_match = re.search(r"\{[\s\S]*\}", text)
        if brace_match:
            text = brace_match.group(0)
        return text

    def _fallback_heuristic_analysis(self, content: str) -> Dict[str, Any]:
        """
        Rule-based heuristic classifier for offline testing, local dev,
        and zero-cost CI environments.
        """
        c = content.lower()

        # Sentiment heuristics
        if any(w in c for w in ["bug", "crash", "error", "崩溃", "乱码", "失败", "吐槽", "卡顿", "问题", "严重"]):
            sentiment = "negative"
        elif any(w in c for w in ["great", "blazing", "fast", "love", "提升", "优秀", "好用", "感谢", "喜欢"]):
            sentiment = "positive"
        else:
            sentiment = "neutral"

        # Category heuristics
        if any(w in c for w in ["bug", "crash", "error", "崩溃", "乱码", "异常", "故障"]):
            category = "bug"
        elif any(w in c for w in ["feature", "request", "建议", "增加", "希望", "加一个", "支持"]):
            category = "feature_request"
        elif any(w in c for w in ["how", "why", "咨询", "怎么", "请问", "如何"]):
            category = "inquiry"
        elif sentiment == "positive":
            category = "praise"
        else:
            category = "other"

        summary = content.strip().split("\n")[0][:120]
        return {
            "sentiment": sentiment,
            "category": category,
            "summary": summary,
            "confidence": 0.85,
        }

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    def _call_openai_api(self, prompt: str) -> str:
        """Call OpenAI-compatible chat completion endpoint via standard HTTP."""
        base_url = self.config.openai_base_url.rstrip("/")
        url = f"{base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            self.logger.error(f"[analyzer] HTTP {e.code} error from LLM API: {err_body}")
            raise

    def analyze_item(self, item: FeedbackItem) -> AnalysisResult:
        """
        Analyze a single FeedbackItem and return validated AnalysisResult.
        """
        if self.config.openai_api_key:
            user_prompt = (
                f"Platform: {item.platform}\n"
                f"Author: {item.author}\n"
                f"Content:\n{item.content}\n"
            )
            raw_output = self._call_openai_api(user_prompt)
            clean_json = self._clean_json_response(raw_output)
            parsed = json.loads(clean_json)
            model_name = self.config.llm_model
        else:
            self.logger.debug(
                f"[analyzer] OPENAI_API_KEY unset. Using deterministic heuristic classifier for item {item.id}"
            )
            parsed = self._fallback_heuristic_analysis(item.content)
            model_name = "heuristic-rule-engine-v1"

        result = AnalysisResult(
            feedback_id=item.id,
            sentiment=parsed.get("sentiment", "neutral"),
            category=parsed.get("category", "other"),
            summary=parsed.get("summary", item.content[:100]),
            confidence=float(parsed.get("confidence", 0.8)),
            model=model_name,
            analyzed_at=current_iso_time(),
        )

        # Validate result contract
        errors = result.validate()
        if errors:
            self.logger.warning(
                f"[analyzer] Contract warning for analysis on {item.id}: {errors}. Normalizing defaults."
            )
            if result.sentiment not in ("positive", "negative", "neutral"):
                result.sentiment = "neutral"
            if result.category not in ("bug", "feature_request", "inquiry", "praise", "other"):
                result.category = "other"

        return result

    def process_pending_feedbacks(self, batch_size: int = 50) -> int:
        """
        Fetch unanalyzed items from DB, run analysis, and store results idempotently.
        Returns count of analyzed items.
        """
        pending_items = self.repo.get_unanalyzed_feedbacks(limit=batch_size)
        if not pending_items:
            self.logger.info("[analyzer] No pending unanalyzed feedback items found in database.")
            return 0

        self.logger.info(f"[analyzer] Processing {len(pending_items)} unanalyzed feedback items...")
        processed_count = 0

        for item in pending_items:
            try:
                analysis = self.analyze_item(item)
                self.repo.save_analysis(analysis)
                processed_count += 1
            except Exception as e:
                self.logger.error(f"[analyzer] Error analyzing feedback {item.id}: {e}")

        self.logger.info(f"[analyzer] Successfully analyzed and saved {processed_count} items.")
        return processed_count
