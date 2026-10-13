"""
LLM-backed structured analysis of user feedback.

Design decision (important, and worth defending in an interview):
    Earlier revisions silently fell back to a keyword-matching "heuristic engine"
    whenever no API key was configured. That is the same failure mode as fabricating
    collected data -- the pipeline looked healthy while every "analysis" in the
    database was actually produced by a handful of `if` statements. This module now
    fails loudly instead, and the rule-based classifier is only reachable when the
    operator explicitly opts in, in which case every row it writes is stamped with
    `model="heuristic-rule-engine-v1"` so it can never be mistaken for LLM output.

Transport: httpx (async), matching the rest of the project's HTTP stack.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import httpx

from src.errors import CollectorConfigError, LLMResponseError
from src.models.schema import AnalysisResult, FeedbackItem, current_iso_time
from src.storage.repository import FeedbackRepository
from src.utils.config import AppConfig
from src.utils.logger import setup_logger
from src.utils.retry import retry_with_backoff

SYSTEM_PROMPT = """You are an expert AI Data Engineering assistant analyzing user feedback.

Analyze the user feedback item and extract structured insights.

Respond ONLY with a valid JSON object strictly matching this schema:
{
  "sentiment": "positive" | "negative" | "neutral",
  "category": "bug" | "feature_request" | "inquiry" | "praise" | "other",
  "summary": "A concise one-sentence summary of the user's core feedback point, in the same language as the input",
  "confidence": 0.0 to 1.0 (float)
}

Do not include any introductory remarks, explanations, or extra commentary outside the JSON object.
"""

VALID_SENTIMENTS = ("positive", "negative", "neutral")
VALID_CATEGORIES = ("bug", "feature_request", "inquiry", "praise", "other")


class LLMAnalyzer:
    """
    Turns raw feedback into a validated, traceable AnalysisResult.

    Guarantees:
      * Structured JSON extraction with fence/prose tolerance.
      * Strict validation against the AnalysisResult contract.
      * Results are linked to feedback_id, preserving end-to-end traceability.
    """

    def __init__(self, config: AppConfig, repo: FeedbackRepository):
        self.config = config
        self.repo = repo
        self.logger = setup_logger("analyzer.llm")

    # ------------------------------------------------------------ JSON handling

    def clean_json_response(self, text: str) -> str:
        """
        Strip markdown fences (```json ... ```) and surrounding prose so the payload
        can be handed to json.loads. Raises LLMResponseError when no object is found.
        """
        if not text or not text.strip():
            raise LLMResponseError("LLM returned an empty response.")

        cleaned = text.strip()
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        brace_match = re.search(r"\{[\s\S]*\}", cleaned)
        if not brace_match:
            raise LLMResponseError(f"No JSON object found in LLM response: {text[:200]!r}")
        return brace_match.group(0)

    def parse_analysis_payload(self, raw_text: str) -> Dict[str, Any]:
        """Parse and normalise an LLM response into the analysis contract fields."""
        payload = json.loads(self.clean_json_response(raw_text))

        sentiment = str(payload.get("sentiment", "")).strip().lower()
        category = str(payload.get("category", "")).strip().lower()

        if sentiment not in VALID_SENTIMENTS:
            raise LLMResponseError(f"LLM returned an invalid sentiment: {sentiment!r}")
        if category not in VALID_CATEGORIES:
            raise LLMResponseError(f"LLM returned an invalid category: {category!r}")

        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise LLMResponseError(
                f"LLM returned a non-numeric confidence: {payload.get('confidence')!r}"
            ) from exc
        confidence = min(max(confidence, 0.0), 1.0)

        summary = str(payload.get("summary", "")).strip()
        if not summary:
            raise LLMResponseError("LLM returned an empty summary.")

        return {
            "sentiment": sentiment,
            "category": category,
            "summary": summary,
            "confidence": confidence,
        }

    # ------------------------------------------------------------- API transport

    @retry_with_backoff(max_retries=3, initial_delay=2.0)
    async def _call_chat_api(self, client: httpx.AsyncClient, prompt: str) -> str:
        """Call an OpenAI-compatible /chat/completions endpoint."""
        url = f"{self.config.openai_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        response = await client.post(url, json=payload)

        if response.status_code in (401, 403):
            raise CollectorConfigError(
                f"LLM API rejected the credentials (HTTP {response.status_code}). "
                "Check OPENAI_API_KEY / OPENAI_BASE_URL in .env."
            )
        response.raise_for_status()

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(
                f"Unexpected LLM response envelope: {json.dumps(data)[:300]}"
            ) from exc

    def _build_prompt(self, item: FeedbackItem) -> str:
        return (
            f"Platform: {item.platform}\n"
            f"Author: {item.author}\n"
            f"Content:\n{item.content}\n"
        )

    # -------------------------------------------------------------- rule engine

    def heuristic_classify(self, content: str) -> Dict[str, Any]:
        """
        Deterministic rule-based classifier.

        NOT used automatically. Reachable only when the operator passes
        --allow-heuristic, and always recorded with model="heuristic-rule-engine-v1"
        so that no downstream reader can confuse it with model output.
        """
        c = (content or "").lower()

        if any(w in c for w in ["bug", "crash", "error", "崩溃", "乱码", "失败", "吐槽", "卡顿", "报错"]):
            sentiment = "negative"
        elif any(w in c for w in ["great", "blazing", "fast", "love", "提升", "优秀", "好用", "感谢", "喜欢"]):
            sentiment = "positive"
        else:
            sentiment = "neutral"

        if any(w in c for w in ["bug", "crash", "error", "崩溃", "乱码", "异常", "故障", "报错"]):
            category = "bug"
        elif any(w in c for w in ["feature", "request", "建议", "增加", "希望", "加一个", "支持"]):
            category = "feature_request"
        elif any(w in c for w in ["how", "why", "咨询", "怎么", "请问", "如何"]):
            category = "inquiry"
        elif sentiment == "positive":
            category = "praise"
        else:
            category = "other"

        return {
            "sentiment": sentiment,
            "category": category,
            "summary": (content or "").strip().split("\n")[0][:120],
            "confidence": 0.85,
        }

    # ------------------------------------------------------------------ public

    async def analyze_item(
        self, client: httpx.AsyncClient, item: FeedbackItem, allow_heuristic: bool = False
    ) -> AnalysisResult:
        """Analyze one item and return a validated AnalysisResult."""
        if self.config.openai_api_key:
            raw_output = await self._call_chat_api(client, self._build_prompt(item))
            parsed = self.parse_analysis_payload(raw_output)
            model_name = self.config.llm_model
        elif allow_heuristic:
            self.logger.warning(
                f"[analyzer] --allow-heuristic is active: {item.id} is being classified by "
                "the rule engine, NOT by an LLM. Results are stamped accordingly."
            )
            parsed = self.heuristic_classify(item.content)
            model_name = "heuristic-rule-engine-v1"
        else:
            raise CollectorConfigError(
                "OPENAI_API_KEY is not configured, so no LLM analysis can be performed. "
                "Set it in .env, or pass --allow-heuristic to run the offline rule engine "
                "(whose output is explicitly labelled as such)."
            )

        result = AnalysisResult(
            feedback_id=item.id,
            sentiment=parsed["sentiment"],
            category=parsed["category"],
            summary=parsed["summary"],
            confidence=parsed["confidence"],
            model=model_name,
            analyzed_at=current_iso_time(),
        )

        errors = result.validate()
        if errors:
            raise LLMResponseError(
                f"Analysis for {item.id} violates the data contract: {errors}"
            )
        return result

    async def process_pending_feedbacks(
        self, batch_size: int = 50, allow_heuristic: bool = False
    ) -> Dict[str, int]:
        """
        Analyze pending items and store results idempotently.

        Returns a counter dict: {"analyzed": n, "failed": n, "skipped": n}.
        """
        pending = self.repo.get_unanalyzed_feedbacks(limit=batch_size)
        if not pending:
            self.logger.info("[analyzer] No pending unanalyzed feedback items found.")
            return {"analyzed": 0, "failed": 0, "skipped": 0}

        self.logger.info(f"[analyzer] Processing {len(pending)} unanalyzed feedback items...")
        analyzed = failed = 0

        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.config.openai_api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        ) as client:
            for item in pending:
                try:
                    analysis = await self.analyze_item(client, item, allow_heuristic=allow_heuristic)
                    self.repo.save_analysis(analysis)
                    analyzed += 1
                except Exception as exc:
                    failed += 1
                    self.logger.error(f"[analyzer] Failed to analyze {item.id}: {exc}")

        self.logger.info(
            f"[analyzer] Done: {analyzed} analyzed, {failed} failed, "
            f"{len(pending) - analyzed - failed} skipped."
        )
        return {"analyzed": analyzed, "failed": failed, "skipped": len(pending) - analyzed - failed}
