import asyncio
import logging

from .bus import Bus, StepCommand, StepResult
from .models import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_RUNNING,
    STEP_COMPLETED,
    STEP_DISPATCHED,
    STEP_FAILED,
    STEP_PENDING,
)
from .store import Store

log = logging.getLogger(__name__)


class Scheduler:
    """Decide which workflow steps may run and dispatch them to drivers.

    Result handlers are invoked concurrently by the bus.  A scheduler-wide lock
    serialises state transitions and scheduling decisions so two simultaneous
    results cannot both dispatch the same newly-ready step.
    """

    # Two total attempts (the initial attempt plus one retry) for failures that
    # the driver explicitly says are retryable.
    MAX_ATTEMPTS = 2

    def __init__(self, store: Store, bus: Bus) -> None:
        self.store = store
        self.bus = bus
        self._lock = asyncio.Lock()

    async def start(self, run_id: str) -> None:
        """Begin executing a run."""
        async with self._lock:
            await self.store.start_run(run_id)
            await self._schedule_running_runs()

    async def handle_result(self, result: StepResult) -> None:
        """Record a driver result and advance any runs that can now make progress.

        The bus may call this method concurrently, so the complete transition is
        protected by ``_lock``.
        """
        async with self._lock:
            run = await self.store.get_run(result.run_id)
            if run.status != RUN_RUNNING:
                log.info(
                    "scheduler: ignoring late result for %s because run %s is %s",
                    result.step_name,
                    result.run_id,
                    run.status,
                )
                return

            # Read the current persisted step so retries and duplicate/late
            # results are decided from executor-owned state, not message fields.
            steps = await self.store.list_steps(result.run_id)
            step = next((st for st in steps if st.id == result.step_id), None)
            if step is None:
                log.warning(
                    "scheduler: result for unknown step %s in run %s",
                    result.step_id,
                    result.run_id,
                )
                return

            if step.status != STEP_DISPATCHED:
                log.info(
                    "scheduler: ignoring result for %s because step is %s",
                    step.name,
                    step.status,
                )
                return

            if result.error:
                if result.retryable and step.dispatch_count < self.MAX_ATTEMPTS:
                    log.warning(
                        "scheduler: step %s failed with retryable error %r; retrying "
                        "(attempt %d/%d)",
                        step.name,
                        result.error,
                        step.dispatch_count + 1,
                        self.MAX_ATTEMPTS,
                    )
                    await self.store.reset_step_for_retry(step.id, result.error)
                else:
                    await self.store.record_step_finished(step.id, STEP_FAILED, result.error)
                    await self.store.finish_run(result.run_id, RUN_FAILED)
                    log.error(
                        "scheduler: run %s failed because step %s failed: %s",
                        result.run_id,
                        result.step_name,
                        result.error,
                    )
                    # The failed driver is free again now. Other runs may have
                    # previously been refused by it, so give them another chance.
                    await self._schedule_running_runs()
                    return
            else:
                await self.store.record_step_finished(step.id, STEP_COMPLETED)

            # A completion (or retryable failure) may unblock this run. Any
            # result also means a device just became free, so retry pending work
            # across all running runs; this prevents busy refusals being forgotten.
            await self._schedule_running_runs()

            # Only this result's run could have become completed because of this
            # result. (Other runs will finish when their own final result arrives.)
            run = await self.store.get_run(result.run_id)
            if run.status == RUN_RUNNING:
                steps = await self.store.list_steps(result.run_id)
                if steps and all(st.status == STEP_COMPLETED for st in steps):
                    await self.store.finish_run(result.run_id, RUN_COMPLETED)
                    log.info("scheduler: run %s completed", result.run_id)

    async def _schedule_running_runs(self) -> None:
        """Try to dispatch every dependency-ready pending step in running runs.

        A refused command is deliberately left pending.  The next result from
        any driver causes all running runs to be reconsidered, so work refused
        because a device was busy is not forgotten, including across runs.
        """
        runs = await self.store.list_runs()
        for run in runs:
            if run.status == RUN_RUNNING:
                await self._schedule_run(run.id)

    async def _schedule_run(self, run_id: str) -> None:
        steps = await self.store.list_steps(run_id)
        by_name = {step.name: step for step in steps}

        # Avoid multiple attempts on the same device in one scheduling pass.
        devices_considered: set[str] = set()

        for step in steps:
            if step.status != STEP_PENDING:
                continue

            if not all(by_name[name].status == STEP_COMPLETED for name in step.depends_on):
                continue

            if step.device_id in devices_considered:
                continue

            state = await self.bus.driver_state(step.device_id)
            if state.busy:
                devices_considered.add(step.device_id)
                continue

            ack = await self.bus.send_command(
                StepCommand(
                    run_id=step.run_id,
                    step_id=step.id,
                    step_name=step.name,
                    device_id=step.device_id,
                )
            )

            devices_considered.add(step.device_id)

            if not ack.accepted:
                log.info(
                    "scheduler: driver %s refused %s: %s",
                    step.device_id,
                    step.name,
                    ack.reason or "busy",
                )
                continue

            # handle_result cannot run this critical section until we release
            # _lock, so even a very fast driver result cannot beat this update.
            await self.store.record_step_dispatched(step.id)
            log.info(
                "scheduler: dispatched %s (step=%s) to %s",
                step.name,
                step.id,
                step.device_id,
            )
