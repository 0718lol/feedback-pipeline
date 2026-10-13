from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def current_iso_time() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FeedbackItem:
    """
    Unified Data Contract for Multi-Platform User Feedback.
    All raw posts from Discord, Xiaohongshu, etc., must conform to this schema
    before entering the persistence layer or downstream analytics.
    """
    id: str
    platform: str
    content: str
    author: str
    created_at: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    url: str = ""
    source_target: str = ""
    raw_data: Dict[str, Any] = field(default_factory=dict)
    collected_at: str = field(default_factory=current_iso_time)

    def validate(self) -> List[str]:
        """
        Validate data contract integrity.
        Returns a list of validation error descriptions. Empty if valid.
        """
        errors: List[str] = []
        if not self.id or not isinstance(self.id, str):
            errors.append("Field 'id' is required and must be a non-empty string.")
        if not self.platform or self.platform not in ("discord", "xhs", "twitter", "mock"):
            errors.append(f"Field 'platform' must be one of ['discord', 'xhs', 'twitter', 'mock'], got: '{self.platform}'.")
        if not self.content or not isinstance(self.content, str) or not self.content.strip():
            errors.append("Field 'content' is required and cannot be empty whitespace.")
        if not self.author or not isinstance(self.author, str):
            errors.append("Field 'author' is required and must be a non-empty string.")
        if not self.created_at:
            errors.append("Field 'created_at' is required (ISO 8601 timestamp).")
        if not self.url or not (self.url.startswith("http://") or self.url.startswith("https://")):
            errors.append(f"Field 'url' must be a valid HTTP(S) URL for traceability, got: '{self.url}'.")
        if not isinstance(self.metrics, dict):
            errors.append("Field 'metrics' must be a dictionary (e.g. {'likes': 0, 'comments': 0}).")
        return errors

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeedbackItem":
        """Construct FeedbackItem from dictionary, parsing nested JSON if needed."""
        metrics = data.get("metrics", {})
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except Exception:
                metrics = {}

        raw_data = data.get("raw_data", {})
        if isinstance(raw_data, str):
            try:
                raw_data = json.loads(raw_data)
            except Exception:
                raw_data = {}

        return cls(
            id=str(data.get("id", "")),
            platform=str(data.get("platform", "")),
            content=str(data.get("content", "")),
            author=str(data.get("author", "")),
            created_at=str(data.get("created_at", "")),
            metrics=metrics,
            url=str(data.get("url", "")),
            source_target=str(data.get("source_target", "")),
            raw_data=raw_data,
            collected_at=str(data.get("collected_at", current_iso_time())),
        )


@dataclass
class AnalysisResult:
    """
    Structured LLM Analysis Result.
    Linked directly to a FeedbackItem by feedback_id for strict end-to-end traceability.
    """
    feedback_id: str
    sentiment: str  # 'positive', 'negative', 'neutral'
    category: str   # 'bug', 'feature_request', 'inquiry', 'praise', 'other'
    summary: str
    confidence: float
    model: str
    analyzed_at: str = field(default_factory=current_iso_time)

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.feedback_id:
            errors.append("Field 'feedback_id' is required.")
        if self.sentiment not in ("positive", "negative", "neutral"):
            errors.append(f"Invalid sentiment '{self.sentiment}'. Must be positive, negative, or neutral.")
        if self.category not in ("bug", "feature_request", "inquiry", "praise", "other"):
            errors.append(f"Invalid category '{self.category}'.")
        if not (0.0 <= self.confidence <= 1.0):
            errors.append(f"Confidence score must be between 0.0 and 1.0, got: {self.confidence}")
        if not self.summary or not self.summary.strip():
            errors.append("Summary cannot be empty.")
        return errors

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisResult":
        return cls(
            feedback_id=str(data.get("feedback_id", "")),
            sentiment=str(data.get("sentiment", "neutral")),
            category=str(data.get("category", "other")),
            summary=str(data.get("summary", "")),
            confidence=float(data.get("confidence", 0.8)),
            model=str(data.get("model", "unknown")),
            analyzed_at=str(data.get("analyzed_at", current_iso_time())),
        )


@dataclass
class CollectorRunStats:
    """Execution telemetry and metrics for monitoring and alerting."""
    run_id: str
    platform: str
    target: str
    status: str  # 'success', 'warning', 'failed'
    items_collected: int = 0
    items_new: int = 0
    items_failed: int = 0
    start_time: str = field(default_factory=current_iso_time)
    end_time: str = ""
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
