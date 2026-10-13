"""
Typed exceptions for the feedback pipeline.

Design note: this project deliberately does NOT silently substitute fake data when
a data source is unreachable, unauthenticated or misconfigured. A collector that
cannot do its job must fail loudly with an actionable message, so that
`collection_runs.status` records a real failure instead of a comfortable zero.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base class for all pipeline errors."""


class CollectorError(PipelineError):
    """A collector failed to complete a target."""


class CollectorConfigError(CollectorError):
    """
    Required configuration is missing or invalid (no token, no keyword, ...).
    Raised before any network activity is attempted.
    """


class LoginRequiredError(CollectorError):
    """
    The target platform served a login wall. The operator must run the
    interactive login flow once; the saved session is then reused.
    """


class PlatformBlockedError(CollectorError):
    """
    The platform returned an anti-bot / verification challenge.
    Per project policy we stop immediately and never attempt to bypass it.
    """


class SchemaViolationError(PipelineError):
    """A record failed validation against the unified data contract."""


class LLMResponseError(PipelineError):
    """The LLM returned a response that could not be parsed into the contract."""
