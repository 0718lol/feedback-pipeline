from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")
logger = logging.getLogger("feedback_pipeline.retry")


def random_sleep(min_seconds: float = 2.0, max_seconds: float = 5.0) -> float:
    """
    Synchronous random jitter delay between min_seconds and max_seconds (default 2 to 5s).
    """
    sleep_duration = random.uniform(min_seconds, max_seconds)
    time.sleep(sleep_duration)
    return sleep_duration


async def async_random_sleep(min_seconds: float = 2.0, max_seconds: float = 5.0) -> float:
    """
    Asynchronous random jitter delay between min_seconds and max_seconds (default 2 to 5s).
    """
    sleep_duration = random.uniform(min_seconds, max_seconds)
    await asyncio.sleep(sleep_duration)
    return sleep_duration


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 2.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    allowed_exceptions: tuple = (Exception,),
):
    """
    Decorator for retry with exponential backoff and jitter.
    Works for both synchronous and asynchronous functions.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                delay = initial_delay
                for attempt in range(1, max_retries + 1):
                    try:
                        return await func(*args, **kwargs)
                    except allowed_exceptions as e:
                        if attempt == max_retries:
                            logger.error(
                                f"[{func.__name__}] Failed after {max_retries} attempts. Last error: {e}"
                            )
                            raise
                        actual_delay = delay + (random.uniform(0.5, 1.5) if jitter else 0.0)
                        logger.warning(
                            f"[{func.__name__}] Attempt {attempt} failed: {e}. "
                            f"Retrying in {actual_delay:.2f}s..."
                        )
                        await asyncio.sleep(actual_delay)
                        delay *= backoff_factor
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                delay = initial_delay
                for attempt in range(1, max_retries + 1):
                    try:
                        return func(*args, **kwargs)
                    except allowed_exceptions as e:
                        if attempt == max_retries:
                            logger.error(
                                f"[{func.__name__}] Failed after {max_retries} attempts. Last error: {e}"
                            )
                            raise
                        actual_delay = delay + (random.uniform(0.5, 1.5) if jitter else 0.0)
                        logger.warning(
                            f"[{func.__name__}] Attempt {attempt} failed: {e}. "
                            f"Retrying in {actual_delay:.2f}s..."
                        )
                        time.sleep(actual_delay)
                        delay *= backoff_factor
            return sync_wrapper

    return decorator
