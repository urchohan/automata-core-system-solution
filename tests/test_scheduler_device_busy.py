"""Tests for device-aware scheduling decisions.

These tests use the real Store and Scheduler but a fake bus, matching the style
of test_scheduler_concurrency.py.  They verify that the scheduler avoids
predictable refusals before a command reaches a worker.
"""

import os

import pytest
from psycopg_pool import AsyncConnectionPool

from services.executor.bus import CommandAck, DriverState, ResultHandler, StepCommand, StepResult
from services.executor.models import RUN_COMPLETED, STEP_DISPATCHED, STEP_PENDING
from services.executor.scheduler import Scheduler
from services.executor.store import Store
from services.executor.workflows import StepTemplate


class DeviceStateBus:
    """Records commands and lets a test control whether a device is busy."""

    def __init__(self) -> None:
        self._commands: list[StepCommand] = []
        self._busy: dict[str, bool] = {}

    def set_busy(self, device_id: str, busy: bool) -> None:
        self._busy[device_id] = busy

    async def send_command(self, cmd: StepCommand) -> CommandAck:
        self._commands.append(cmd)
        return CommandAck(accepted=True)

    async def on_step_result(self, handler: ResultHandler) -> None:
        return None

    async def driver_state(self, device_id: str) -> DriverState:
        return DriverState(
            device_id=device_id,
            busy=self._busy.get(device_id, False),
            current_step="other-step" if self._busy.get(device_id, False) else "",
        )

    async def close(self) -> None:
        return None

    def sent_for_run(self, run_id: str) -> list[StepCommand]:
        return [cmd for cmd in self._commands if cmd.run_id == run_id]


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set -- run with: docker compose run --rm tests")
    return url


@pytest.fixture
async def pool(database_url: str):
    async with AsyncConnectionPool(database_url, min_size=2, max_size=10, open=False) as p:
        await p.wait()
        yield p


async def test_busy_device_is_not_commanded(pool) -> None:
    store = Store(pool)
    run = await store.create_run(
        "busy-device-test",
        [StepTemplate(name="work", device_id="incubator-1")],
    )

    bus = DeviceStateBus()
    bus.set_busy("incubator-1", True)
    sched = Scheduler(store, bus)

    await sched.start(run.id)

    assert bus.sent_for_run(run.id) == []
    steps = await store.list_steps(run.id)
    assert len(steps) == 1
    assert steps[0].status == STEP_PENDING
    assert steps[0].dispatch_count == 0


async def test_only_one_ready_step_per_device_is_sent_per_pass(pool) -> None:
    store = Store(pool)
    run = await store.create_run(
        "same-device-test",
        [
            StepTemplate(name="alpha", device_id="liquid-handler-1"),
            StepTemplate(name="beta", device_id="liquid-handler-1"),
        ],
    )

    bus = DeviceStateBus()
    sched = Scheduler(store, bus)

    await sched.start(run.id)

    first_batch = bus.sent_for_run(run.id)
    assert len(first_batch) == 1

    steps = await store.list_steps(run.id)
    statuses = {step.status for step in steps}
    assert statuses == {STEP_PENDING, STEP_DISPATCHED}

    first = first_batch[0]
    await sched.handle_result(
        StepResult(
            run_id=first.run_id,
            step_id=first.step_id,
            step_name=first.step_name,
            device_id=first.device_id,
        )
    )

    second_batch = bus.sent_for_run(run.id)
    assert len(second_batch) == 2
    second = second_batch[1]
    assert second.step_id != first.step_id

    await sched.handle_result(
        StepResult(
            run_id=second.run_id,
            step_id=second.step_id,
            step_name=second.step_name,
            device_id=second.device_id,
        )
    )

    finished = await store.get_run(run.id)
    assert finished.status == RUN_COMPLETED
