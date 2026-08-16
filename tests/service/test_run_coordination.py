import asyncio

import pytest

from service import run_coordination
from service.run_coordination import serialize_thread_run


@pytest.mark.asyncio
async def test_same_thread_runs_are_serialized() -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with serialize_thread_run("agent", "thread"):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second() -> None:
        await first_entered.wait()
        async with serialize_thread_run("agent", "thread"):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-exit", "second-enter"]


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_slot_user() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with serialize_thread_run("agent", "cancelled"):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with serialize_thread_run("agent", "cancelled"):
            raise AssertionError("cancelled waiter entered the lock")

    holder_task = asyncio.create_task(holder())
    await entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    release.set()
    await holder_task
    assert ("agent", "cancelled") not in run_coordination._slots
