# Notes

I spent approximately **8–10 hours** on the exercise.

## What I achieved

I completed the main scheduler requirements and added some extra reliability work.

For a fuller walkthrough of the final design, diagrams, requirement mapping, and additional test coverage, see [`SOLUTION.md`](./SOLUTION.md).

The scheduler now:

- starts a run and finds steps whose dependencies are complete;
- allows independent work on different devices to run in parallel;
- avoids sending more work to a device that is already busy;
- still treats `CommandAck` as the final answer on whether a command was accepted;
- records accepted work as `dispatched`;
- completes steps only when a successful `StepResult` arrives;
- fails the run when a step has a terminal failure;
- retries a failure once when the driver says it is retryable;
- ignores duplicate or late results safely;
- protects scheduler state changes with one `asyncio.Lock` so simultaneous results do not dispatch the same next step twice.

I also added two levels of extra testing:

- focused `pytest` tests for scheduler decisions and state changes;
- end-to-end bash tests using the real executor, Postgres, NATS and device containers.

The supplied acceptance, failure and concurrency checks pass. My additional deterministic tests also pass, and a random-failure stress run completed without dependency or retry-limit violations.

## How I approached the work

I kept the solution event-driven. A scheduling pass happens when a run starts and whenever a result arrives.

A pending step is ready only when all of its dependencies are complete. Before dispatching it, I check the driver state. This avoids predictable busy refusals, but it is only a pre-check: the device can change state before the command arrives, so the returned `CommandAck` is still the final admission decision.

My background is mainly C, Java and MATLAB, with less Python experience. I spent extra time understanding Python `asyncio`, the supplied codebase, and setting up Docker/WSL/VS Code debugging.

I used ChatGPT extensively as a development aid, as allowed by the brief. I used it to understand unfamiliar Python/asyncio code, reason about concurrency and failure cases, debug the environment, and design additional tests. I reviewed and exercised the final implementation and can explain the choices made.

## 1. What I deliberately did not build

I did not add automatic recovery for a dropped `StepResult`.

Repeating a physical operation, especially liquid handling, may be unsafe if the device actually completed the work but the result message was lost.

## 2. Where the implementation is most likely to break

The implementation is most likely to break at the boundary between the executor's database state and what actually happened on a device.

The `asyncio.Lock` also protects only one executor process. Multiple executor replicas would need database-level coordination.

## 3. If two drivers finish at the same time

If two drivers report completion at the same time, both result handlers may start concurrently, but the scheduler lock makes them process state changes one at a time. Device work itself still remains concurrent.

## 4. If an instrument works but never reports back

In the current implementation, the step remains `dispatched` and the run can remain `running` indefinitely because no later event tells the scheduler how the step ended.

A device can finish the physical work and become idle while the executor still thinks the step is `dispatched`. From the current protocol, the executor cannot know whether the lost result meant success or failure.

I added a probe that demonstrates this exact limitation. A production system would need durable command/result IDs, timeout-based reconciliation, stored results, and a clear rule for whether a physical command can safely be repeated.

## Feedback

The exercise was clear and the failure/drop controls were useful. The suggested two hours was enough to identify the core task, but I spent longer because Python/asyncio was less familiar to me, local setup took time, and I chose to test the solution under more failure cases than the minimum required.
