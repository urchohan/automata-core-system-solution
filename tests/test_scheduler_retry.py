"""Tests for bounded retry and permanent failure handling."""

import os

import pytest
from psycopg_pool import AsyncConnectionPool

from services.executor.bus import CommandAck, DriverState, ResultHandler, StepCommand, StepResult
from services.executor.models import RUN_FAILED, STEP_FAILED
from services.executor.scheduler import Scheduler
from services.executor.store import Store
from services.executor.workflows import StepTemplate


class RecordingBus:
    """Accepts every command and records what the scheduler dispatched."""

    def __init__(self) -> None:
        self._commands: list[StepCommand] = []

    async def send_command(self, cmd: StepCommand) -> CommandAck:
        self._commands.append(cmd)
        return CommandAck(accepted=True)

    async def on_step_result(self, handler: ResultHandler) -> None:
        return None

    async def driver_state(self, device_id: str) -> DriverState:
        return DriverState(device_id=device_id)

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


async def test_retryable_failure_is_retried_once_then_fails(pool) -> None:
    store = Store(pool)
    run = await store.create_run(
        "retry-test",
        [StepTemplate(name="incubate", device_id="incubator-1")],
    )

    bus = RecordingBus()
    sched = Scheduler(store, bus)
    await sched.start(run.id)

    first = bus.sent_for_run(run.id)
    assert len(first) == 1

    await sched.handle_result(
        StepResult(
            run_id=run.id,
            step_id=first[0].step_id,
            step_name=first[0].step_name,
            device_id=first[0].device_id,
            error="temporary instrument error",
            retryable=True,
        )
    )

    after_retry = bus.sent_for_run(run.id)
    assert len(after_retry) == 2
    assert after_retry[1].step_id == first[0].step_id

    steps = await store.list_steps(run.id)
    assert len(steps) == 1
    assert steps[0].dispatch_count == Scheduler.MAX_ATTEMPTS

    await sched.handle_result(
        StepResult(
            run_id=run.id,
            step_id=after_retry[1].step_id,
            step_name=after_retry[1].step_name,
            device_id=after_retry[1].device_id,
            error="temporary instrument error again",
            retryable=True,
        )
    )

    assert len(bus.sent_for_run(run.id)) == Scheduler.MAX_ATTEMPTS

    failed_run = await store.get_run(run.id)
    assert failed_run.status == RUN_FAILED

    steps = await store.list_steps(run.id)
    assert steps[0].status == STEP_FAILED
    assert steps[0].dispatch_count == Scheduler.MAX_ATTEMPTS


async def test_non_retryable_failure_fails_without_redispatch(pool) -> None:
    store = Store(pool)
    run = await store.create_run(
        "non-retryable-test",
        [StepTemplate(name="dispense", device_id="liquid-handler-1")],
    )

    bus = RecordingBus()
    sched = Scheduler(store, bus)
    await sched.start(run.id)

    commands = bus.sent_for_run(run.id)
    assert len(commands) == 1

    await sched.handle_result(
        StepResult(
            run_id=run.id,
            step_id=commands[0].step_id,
            step_name=commands[0].step_name,
            device_id=commands[0].device_id,
            error="partially dispensed plate",
            retryable=False,
        )
    )

    assert len(bus.sent_for_run(run.id)) == 1

    failed_run = await store.get_run(run.id)
    assert failed_run.status == RUN_FAILED

    steps = await store.list_steps(run.id)
    assert len(steps) == 1
    assert steps[0].status == STEP_FAILED
    assert steps[0].dispatch_count == 1
