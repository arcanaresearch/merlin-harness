"""Small asynchronous batches for independent model review requests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass


@dataclass(slots=True)
class _PendingReview[InputT, OutputT]:
    item: InputT
    future: asyncio.Future[OutputT]


class AsyncReviewMicrobatcher[InputT, OutputT]:
    """Coalesce nearby review requests while preserving one result per input."""

    def __init__(
        self,
        handler: Callable[[Hashable, tuple[InputT, ...]], Awaitable[tuple[OutputT, ...]]],
        *,
        maximum_batch_size: int,
        maximum_wait_seconds: float,
    ) -> None:
        if maximum_batch_size < 1:
            raise ValueError("review batch size must be positive")
        if maximum_wait_seconds <= 0:
            raise ValueError("review batch wait must be positive")
        self._handler = handler
        self._maximum_batch_size = maximum_batch_size
        self._maximum_wait_seconds = maximum_wait_seconds
        self._lock = asyncio.Lock()
        self._pending: dict[Hashable, list[_PendingReview[InputT, OutputT]]] = {}
        self._timers: dict[Hashable, asyncio.Task[None]] = {}

    async def submit(self, key: Hashable, item: InputT) -> OutputT:
        """Queue one item and return its corresponding typed review."""

        future = asyncio.get_running_loop().create_future()
        batch: tuple[_PendingReview[InputT, OutputT], ...] = ()
        async with self._lock:
            pending = self._pending.setdefault(key, [])
            pending.append(_PendingReview(item=item, future=future))
            if len(pending) == 1:
                self._timers[key] = asyncio.create_task(self._flush_after_wait(key))
            if len(pending) >= self._maximum_batch_size:
                batch = self._take_locked(key)
        if batch:
            asyncio.create_task(self._run(key, batch))
        return await future

    async def _flush_after_wait(self, key: Hashable) -> None:
        try:
            await asyncio.sleep(self._maximum_wait_seconds)
            async with self._lock:
                batch = self._take_locked(key)
            if batch:
                await self._run(key, batch)
        except asyncio.CancelledError:
            return

    def _take_locked(self, key: Hashable) -> tuple[_PendingReview[InputT, OutputT], ...]:
        pending = self._pending.pop(key, [])
        timer = self._timers.pop(key, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        return tuple(pending)

    async def _run(
        self,
        key: Hashable,
        batch: tuple[_PendingReview[InputT, OutputT], ...],
    ) -> None:
        try:
            results = await self._handler(key, tuple(entry.item for entry in batch))
            if len(results) != len(batch):
                raise ValueError("review batch returned a different number of results")
        except BaseException as error:
            for entry in batch:
                if not entry.future.done():
                    entry.future.set_exception(error)
            return
        for entry, result in zip(batch, results, strict=True):
            if not entry.future.done():
                entry.future.set_result(result)
