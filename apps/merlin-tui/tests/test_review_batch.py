import asyncio

import pytest
from merlin_tui.review_batch import AsyncReviewMicrobatcher

pytestmark = pytest.mark.asyncio


async def test_microbatcher_coalesces_requests_and_preserves_result_order() -> None:
    batch_sizes: list[int] = []
    active = 0
    maximum_active = 0
    concurrency = asyncio.Semaphore(2)

    async def handle(_key, items):
        nonlocal active, maximum_active
        async with concurrency:
            active += 1
            maximum_active = max(maximum_active, active)
            batch_sizes.append(len(items))
            await asyncio.sleep(0)
            active -= 1
            return tuple(item * 10 for item in items)

    batcher = AsyncReviewMicrobatcher(
        handle,
        maximum_batch_size=4,
        maximum_wait_seconds=0.01,
    )
    results = await asyncio.gather(*(batcher.submit("campaign/wave", item) for item in range(10)))

    assert results == [item * 10 for item in range(10)]
    assert sorted(batch_sizes) == [2, 4, 4]
    assert maximum_active <= 2


async def test_microbatcher_fans_handler_failure_out_and_accepts_later_work() -> None:
    attempts = 0

    async def handle(_key, items):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("review provider unavailable")
        return tuple(item.upper() for item in items)

    batcher = AsyncReviewMicrobatcher(
        handle,
        maximum_batch_size=2,
        maximum_wait_seconds=0.01,
    )
    first = await asyncio.gather(
        batcher.submit("campaign/wave", "one"),
        batcher.submit("campaign/wave", "two"),
        return_exceptions=True,
    )
    assert all(isinstance(item, RuntimeError) for item in first)
    assert await batcher.submit("campaign/wave", "three") == "THREE"
