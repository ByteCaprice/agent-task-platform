# Tools and Skills

Tools and Skills solve different problems.

## Tools

A Tool performs a capability call. Supported endpoints include Python targets, HTTP endpoints, MCP servers, and built-ins. Tool Gateway enforces Agent permissions, JSON Schema validation, timeouts, retries, concurrency, QPS limits, circuit breaking, and audit logs.

Use a Tool for an action or data lookup. Treat Python targets as trusted code: registering a Python target allows code execution in the server process or configured isolation runtime.

## Skills

A Skill is a versioned package of instructions, references, assets, and optional governed scripts. Before execution, the platform resolves configured Skills into immutable snapshots attached to the Run.

Skills can be always active, selected automatically, or activated explicitly. Skill scripts are not automatically exposed to model output; they require explicit registration and remain subject to runtime timeout controls.

Use a Skill for task guidance and reusable local knowledge. Use a Tool for an externally observable capability.
