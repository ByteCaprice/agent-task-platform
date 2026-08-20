# Durable Stages

Durable stages let an Agent split long or multi-step work into persisted checkpoints.

Each stage stores an input hash, definition version, status, attempt metadata, checkpoint, output, and error. When a Run retries or is recovered after interruption, completed stages can be reused instead of executed again.

Use stages for work where replaying a completed step would be expensive or unsafe. Give every stage a stable key and increment its definition version when its behavior or output contract changes.

For an external side effect, use the platform's side-effect operation protections and an idempotency key. If the platform cannot determine whether a side effect completed, it treats the outcome as unknown rather than replaying it automatically.

Child Agent work can also be represented as a durable stage. The parent pins the resolved child Agent version so later registry changes do not change an in-flight workflow.
