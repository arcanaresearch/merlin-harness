from __future__ import annotations

import asyncio
import inspect
from typing import Any


class ResourceCloser:
    """Closes registered resources once, in reverse construction order."""

    def __init__(self, *resources: object) -> None:
        self._resources = list(resources)
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for resource in reversed(self._resources):
            close = (
                getattr(resource, "aclose", None)
                or getattr(resource, "close", None)
                or getattr(resource, "stop", None)
            )
            if close is None:
                continue
            try:
                if inspect.iscoroutinefunction(close):
                    result = await close()
                else:
                    result = await asyncio.to_thread(close)
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("resource shutdown failed", errors)

    async def __aenter__(self) -> ResourceCloser:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await asyncio.shield(self.close())
