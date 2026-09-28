"""Tests for result idempotency and run terminal-state handling."""

import os

import pytest
from psycopg_pool import AsyncConnectionPool

from services.executor.bus import CommandAck, DriverState, ResultHandler, StepCommand, StepResult
from services.executor.models import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    STEP_COMPLETED,
    STEP_DISPATCHED,
    STEP_PENDING,
)
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


async def test_duplicate_result_is_ignored(pool) -> None:
    store = Store(pool)
    run = await store.create_run(
        "duplicate-result-test",
        [StepTemplate(name="read", device_id="plate-reader-1")],
    )

    bus = RecordingBus()
    sched = Scheduler(store, bus)
    await sched.start(run.id)

    commands = bus.sent_for_run(run.id)
    assert len(commands) == 1

    result = StepResult(
        run_id=run.id,
        step_id=commands[0].step_id,
        step_name=commands[0].step_name,
        device_id=commands[0].device_id,
    )

    await sched.handle_result(result)
    await sched.handle_result(result)

    assert len(bus.sent_for_run(run.id)) == 1

    finished_run = await store.get_run(run.id)
    assert finished_run.status == RUN_COMPLETED

    steps = await store.list_steps(run.id)
    assert len(steps) == 1
    assert steps[0].status == STEP_COMPLETED
    assert steps[0].dispatch_count == 1


async def test_late_result_after_failed_run_is_ignored(pool) -> None:
    store = Store(pool)
    run = await store.create_run(
        "late-result-test",
        [
            StepTemplate(name="dispense", device_id="liquid-handler-1"),
            StepTemplate(name="incubate", device_id="incubator-1"),
            StepTemplate(
                name="report",
                device_id="plate-reader-1",
                depends_on=["dispense", "incubate"],
            ),
        ],
    )

    bus = RecordingBus()
    sched = Scheduler(store, bus)
    await sched.start(run.id)

    commands = bus.sent_for_run(run.id)
    assert len(commands) == 2
    by_name = {cmd.step_name: cmd for cmd in commands}

    await sched.handle_result(
        StepResult(
            run_id=run.id,
            step_id=by_name["dispense"].step_id,
            step_name="dispense",
            device_id=by_name["dispense"].device_id,
            error="non-retryable liquid handling failure",
            retryable=False,
        )
    )

    failed_run = await store.get_run(run.id)
    assert failed_run.status == RUN_FAILED

    await sched.handle_result(
        StepResult(
            run_id=run.id,
            step_id=by_name["incubate"].step_id,
            step_name="incubate",
            device_id=by_name["incubate"].device_id,
        )
    )

    assert len(bus.sent_for_run(run.id)) == 2

    still_failed = await store.get_run(run.id)
    assert still_failed.status == RUN_FAILED

    steps = {step.name: step for step in await store.list_steps(run.id)}
    assert steps["incubate"].status == STEP_DISPATCHED
    assert steps["report"].status == STEP_PENDING


async def test_run_completes_only_after_all_steps_complete(pool) -> None:
    store = Store(pool)
    run = await store.create_run(
        "completion-test",
        [
            StepTemplate(name="dispense", device_id="liquid-handler-1"),
            StepTemplate(name="incubate", device_id="incubator-1"),
        ],
    )

    bus = RecordingBus()
    sched = Scheduler(store, bus)
    await sched.start(run.id)

    commands = bus.sent_for_run(run.id)
    assert len(commands) == 2

    await sched.handle_result(
        StepResult(
            run_id=run.id,
            step_id=commands[0].step_id,
            step_name=commands[0].step_name,
            device_id=commands[0].device_id,
        )
    )

    halfway = await store.get_run(run.id)
    assert halfway.status == RUN_RUNNING

    await sched.handle_result(
        StepResult(
            run_id=run.id,
            step_id=commands[1].step_id,
            step_name=commands[1].step_name,
            device_id=commands[1].device_id,
        )
    )

    finished = await store.get_run(run.id)
    assert finished.status == RUN_COMPLETED

    steps = await store.list_steps(run.id)
    assert all(step.status == STEP_COMPLETED for step in steps)
