import asyncio

from pr_review_agent.scan.runner import _Budget


async def test_budget_waits_for_running_chunks_instead_of_giving_up():
    b = _Budget(3.0)
    first = await b.reserve(2.0)
    second = await b.reserve(2.0)
    assert (first, second) == (2.0, 1.0)
    waiter = asyncio.create_task(b.reserve(2.0))
    await asyncio.sleep(0)
    assert not waiter.done()  # everything is reserved by running chunks
    await b.settle(first, 0.2)  # the first chunk only spent $0.20
    assert abs(await waiter - 1.8) < 1e-9
    await b.settle(second, 0.2)
    await b.settle(1.8, 2.6 + 1e-9)
    assert await b.reserve(2.0) == 0.0  # spent 3.0 of 3.0: exhausted, nothing running
