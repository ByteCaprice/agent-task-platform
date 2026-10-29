# Pi Agent Runtime

The `pi` runtime uses the open-source [Pi agent core](https://github.com/earendil-works/pi) for the model/tool loop while Agent Task Platform continues to own Runs, routing, retries, cancellation, durable stages, Tool Gateway policy, Skills, callbacks, and audit records.

The integration pins `@earendil-works/pi-agent-core@1.4.0` and `@earendil-works/pi-ai@1.4.0`; the upstream packages are MIT licensed. A task-scoped local Node process runs Pi core and communicates with Python over a bounded JSONL protocol. It does not use the Pi coding-agent CLI, discover local resources, or receive filesystem and shell tools.

## Execution flow

```text
RunExecutor (attempt timeout and cancellation)
  -> AgentRuntime (AgentContext and SkillSession)
    -> PiAgent (durable pi-agent stage and output validation)
      -> PiProcess (Node lifecycle and JSONL protocol)
        -> Pi agent core
          -> prepare_request: compose current Skill instructions and open an audit record
          -> pi-ai: call an OpenAI-compatible Chat Completions endpoint
          -> model_end: persist bounded usage and request metadata
          -> tool_call: invoke Tool Gateway or the Run's SkillSession in Python
          -> next turn or final answer
```

The model request is made by `pi-ai` in Node rather than `ModelGateway.complete()`. Pi takes a snapshot of the current platform model defaults at the start of each attempt, including defaults reloaded from database-managed configuration. Model-call records are still persisted through the platform store.

## Installation

Pi Agents require Node.js 22.19 or newer. Other runtime types do not require Node.

The current Pi transport uses POSIX process groups for descendant cleanup and is supported on Linux and macOS. Other platform runtimes remain importable without Node, but Pi execution on Windows has not been implemented or validated.

```bash
npm ci --ignore-scripts --prefix framework/runtime/pi/node
node framework/runtime/pi/node/runner.mjs --check
```

Wheels include the runner and npm lockfile, but not `node_modules`. After installing a wheel, install the locked dependencies into its packaged asset directory:

```bash
PI_DIR=$(python -c 'from framework.runtime.pi.transport import NODE_DIR; print(NODE_DIR)')
npm ci --ignore-scripts --prefix "$PI_DIR"
node "$PI_DIR/runner.mjs" --check
```

The service `PATH` must contain Node. Runtime requests never install dependencies from the network.

## Agent configuration

```yaml
runtime:
  type: pi
  instructions: Use the assigned tools and return JSON.
  max_turns: 10
  max_tool_calls: 100
  max_parallel_tools: 8
  tool_execution: parallel
  thinking_level: off
  allow_skill_scripts: false
  model_config:
    model: provider/model-id
    fallback_models: []
    max_tokens: 4096
    context_window: 128000
    timeout_seconds: 60
    max_retries: 2
```

Model settings are resolved in this order: `runtime.model_config`, top-level runtime fields, then the platform model configuration snapshot. A model and HTTP(S) `base_url` must be available. Pi currently uses OpenAI-compatible Chat Completions directly from the Node worker.

Loop settings:

- `max_turns` defaults to 10 and includes the final-answer turn. If the last allowed turn calls Tools, those calls can complete, but the attempt then fails because another model turn would be required.
- `max_tool_calls` defaults to 100. A complete over-budget batch is rejected before any Tool in that batch is dispatched.
- `max_parallel_tools` defaults to 8 and accepts 1–64. It is enforced in both Node and Python and does not replace platform-wide Tool concurrency or QPS limits.
- `tool_execution` is `parallel` or `sequential`. Pi preserves the model's Tool-call order when adding results to the transcript.
- `thinking_level` accepts `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`; the selected endpoint must support it.
- `prompt` / `instructions`, `prompt_name` / `prompt_version`, and `input_template` retain their existing meanings. `skill_catalog_max_chars` defaults to 8,000.
- `max_tokens` defaults to 4,096, `context_window` to 128,000, request timeout to 60 seconds, temperature to 0, and model retries to 2. The request timeout covers the entire retry/SSE lifetime, not only response headers.

Current boundaries:

- Model fallback chains and provider-native tools, plugins, and web search are not supported by this adapter.
- `output_type` may reference a Python type as `module:attribute`. Its JSON Schema is included in the prompt, and the final response is validated with Pydantic.
- `response_format` is passed only when explicitly configured and must be supported by the endpoint.
- Only tools listed by `AgentSpec.tools` are exposed. Every call returns through Tool Gateway.
- Assigned Skills expose `skill_load` and `skill_read_resource`. `skill_run_script` requires `allow_skill_scripts: true`.
- Pi does not scan `.pi`, home-directory, or project Skill directories. It receives only the immutable Skills assigned to the current Run.

`agent_hub/pi_multistep_demo/` and `plugins/tools/pi_demo.py` provide a disabled, synthetic example that exercises dependent Tool calls, model-visible Tool errors, recovery, and structured output without connecting to a business system.

## Recovery and safety

The whole Pi attempt is one durable `pi-agent` stage. A completed stage is reused during recovery. An interrupted attempt restarts its model loop; individual model turns are audit records, not checkpoints, so read-only tools may run again.

Normal Tool failures are returned to the model. Cancellation, side-effect outcome-unknown errors, database/audit failures, malformed protocol records, model failures, limit exhaustion, and invalid final output fail the attempt instead.

The bridge enforces these resource boundaries:

- 4 MiB of streamed model deltas per turn.
- 16 MiB per provider request body and per decompressed provider response body.
- 8 MiB per Python/Node JSONL record and at most 128 pending capability requests.
- Positive 32-bit bounds for turn, token, byte-count, latency, and related persisted metrics.

It rejects incomplete or out-of-order protocol records, non-finite JSON, invalid Unicode text, NUL characters, and output that PostgreSQL JSONB cannot store. Tool results that exceed the protocol limit or cannot be represented as finite JSON fail the attempt instead of being truncated or rewritten.

Raw prompts, transcripts, reasoning, provider error bodies, child stderr, and generated output keys are not copied into logs. Tool-call identifiers are logged only as hashes. Provider request IDs are accepted only as bounded identifier strings. Model-call metadata includes the turn, message count, stop reason, HTTP status, request ID, provider latency, HTTP attempts, and request/response byte counts.

The Node process receives a small environment allowlist for path, locale, proxy, and certificate settings. It does not inherit arbitrary application credentials or Node preload hooks. Model credentials are sent only in the task-scoped JSONL configuration. The child process is isolated for lifecycle management, but it is not a security sandbox.

Cancellation aborts pending Python capability requests and then attempts a graceful child abort before terminating and, if needed, killing the process group. Tools must still cooperate with Python cancellation; arbitrary in-process Python code cannot be safely force-killed.

The first implementation intentionally does not persist the complete transcript, resume an individual model turn, compact context, provide mid-run steering, or pool Node workers. Deployments should measure process count, memory, startup latency, and throughput before enabling high Pi concurrency.

## Migrating from `openai_agents`

The OpenAI Agents SDK adapter and Python optional dependency were removed. Migration is explicit because the runtime behavior and supported options differ:

1. Drain or cancel unfinished Runs that use `openai_agents`.
2. Install Node and the locked npm dependencies on API and Worker hosts.
3. Create a new Agent version with `runtime.type: pi`.
4. Review the exact model ID, output contract, assigned tools, turn budgets, and model settings.
5. Do not automatically add or remove an `openai/` model prefix. Pi sends the exact configured endpoint model ID.
6. Remove SDK-specific tracing, workflow, and strict-schema settings. Rename the old `enable_skill_scripts` compatibility field to `allow_skill_scripts` if needed.
7. Set `fallback_models: []`; fallback chains, provider-native Tools, search, and plugins need a separate migration plan.
8. Validate Tool recovery, output schema, usage records, cancellation, and callbacks with synthetic inputs before routing traffic to it.

Do not rename old `openai-agents` stages or rewrite historical Runs. This release raises a migration error if an Agent registration still selects `openai_agents`.

## Tests

The Pi suites use the real npm loop with local synthetic SSE servers; they do not call paid model APIs:

```bash
uv run pytest --noconftest -q \
  tests/test_pi_runtime.py \
  tests/test_pi_runtime_edges.py \
  tests/test_pi_production.py \
  tests/test_pi_failure_boundaries.py \
  tests/test_pi_multistep_demo.py
```

`tests/test_pi_platform_integration.py` additionally requires the repository's PostgreSQL/Testcontainers setup.
