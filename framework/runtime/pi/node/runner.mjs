// One task-scoped Pi core. No CLI, resource discovery, filesystem tools, or stored credentials.
import { Agent } from "@earendil-works/pi-agent-core";
import { normalizeContext } from "@earendil-works/pi-ai";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";
import { createInterface } from "node:readline";
import { boundedFetch } from "./http.mjs";

const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 19)) {
  throw new Error("Pi requires Node.js >=22.19.0");
}

if (process.argv.includes("--check")) {
  process.stdout.write(JSON.stringify({ pi: "1.4.0", protocol: 1 }) + "\n");
} else {
  await serve();
}

async function serve() {
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  const pending = new Map();
  let sequence = 0;
  let agent;
  let started = false;
  let finished = false;
  let output = Promise.resolve();
  let aborted = false;
  let activeTools = 0;
  let maxParallelTools;
  const toolWaiters = [];

  // Serialize concurrent tool responses and honor pipe backpressure.
  function send(record) {
    const line = JSON.stringify(record) + "\n";
    if (Buffer.byteLength(line) > 8 * 1024 * 1024) {
      return Promise.reject(new Error("protocol_record_too_large"));
    }
    output = output.then(() => new Promise((resolve, reject) => {
      process.stdout.write(line, (error) => error ? reject(error) : resolve());
    }));
    return output;
  }

  function abort() {
    aborted = true;
    agent?.abort();
    for (const request of pending.values()) request.reject(new Error("aborted"));
    pending.clear();
    for (const waiter of toolWaiters.splice(0)) waiter.reject(new Error("aborted"));
  }

  function request(kind, data) {
    if (aborted) return Promise.reject(new Error("aborted"));
    const id = String(++sequence);
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      send({ type: "request", id, kind, ...data }).catch((error) => {
        pending.delete(id);
        reject(error);
      });
    });
  }

  async function withToolSlot(signal, operation) {
    signal?.throwIfAborted();
    if (activeTools < maxParallelTools) activeTools++;
    else await new Promise((resolve, reject) => toolWaiters.push({ resolve, reject }));
    try {
      signal?.throwIfAborted();
      return await operation();
    } finally {
      // Transfer the reserved slot to the next waiter without allowing a new caller to steal it.
      const waiter = toolWaiters.shift();
      if (waiter) waiter.resolve();
      else activeTools--;
    }
  }

  async function run(config) {
    if (config.protocol !== 1) throw new Error("protocol_version");
    maxParallelTools = config.max_parallel_tools;
    const modelConfig = config.model;
    const model = {
      id: modelConfig.model, name: modelConfig.model,
      api: "openai-completions", provider: "agent-task-platform", baseUrl: modelConfig.base_url,
      reasoning: config.thinking_level !== "off", input: ["text"],
      contextWindow: modelConfig.context_window, maxTokens: modelConfig.max_tokens,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      compat: { supportsDeveloperRole: false, supportsStore: false, maxTokensField: "max_tokens" },
    };
    const tools = config.tools.map((tool) => ({
      ...tool, label: tool.name,
      execute: async (callId, args, signal) => withToolSlot(signal, async () => {
        signal?.throwIfAborted();
        const response = await request("tool_call", {
          name: tool.name, arguments: args, tool_call_id: callId, turn,
        });
        return {
          content: [{ type: "text", text: response.is_error ? response.text : JSON.stringify(response.value) }],
          details: {}, isError: response.is_error,
        };
      }),
    }));
    let turn = 0;
    let failureCode;
    let requestActive = false;
    let responseStatus;
    let providerRequestId;
    let requestStarted;
    let requestTimer;
    let requestTimedOut = false;
    let responseBytes = 0;
    let toolCalls = 0;
    let httpStats;
    agent = new Agent({
      initialState: { model, systemPrompt: config.instructions, tools, thinkingLevel: config.thinking_level },
      identity: { attribution: null },
      sessionId: config.run_id,
      toolExecution: config.tool_execution,
      streamFn: (selectedModel, context, options) => {
        // Provider client timeouts may stop at response headers. Bound the entire SSE/retry lifetime.
        const deadline = new AbortController();
        const stats = httpStats;
        requestTimer = setTimeout(() => {
          requestTimedOut = true;
          deadline.abort();
        }, modelConfig.timeout_seconds * 1000);
        return streamSimple(selectedModel, normalizeContext(context), {
          ...options,
          signal: AbortSignal.any([options.signal, deadline.signal].filter(Boolean)),
          apiKey: modelConfig.api_key || "unused",
          temperature: modelConfig.temperature, maxTokens: modelConfig.max_tokens,
          timeoutMs: modelConfig.timeout_seconds * 1000, maxRetries: modelConfig.max_retries,
          fetch: boundedFetch({
            onRequest: (bytes) => {
              stats.http_attempts++;
              stats.request_bytes += bytes;
            },
            onResponseBytes: (bytes) => { stats.response_bytes += bytes; },
            onResponse: (response) => {
              responseStatus = response.status;
              providerRequestId = response.headers.get("x-request-id") || response.headers.get("request-id");
            },
            onLimit: (code) => {
              failureCode = code;
              deadline.abort();
            },
          }),
          onPayload: (payload) => ({ ...payload,
            ...(modelConfig.response_format ? { response_format: modelConfig.response_format } : {}),
          }),
        });
      },
      prepareRequest: async ({ context }, signal) => {
        signal?.throwIfAborted();
        if (turn >= config.max_turns) {
          failureCode = "max_turns_exceeded";
          throw new Error(failureCode);
        }
        const response = await request("prepare_request", {
          turn: ++turn, message_count: context.messages.length,
        });
        signal?.throwIfAborted();
        requestActive = true;
        responseStatus = undefined;
        providerRequestId = undefined;
        requestTimedOut = false;
        responseBytes = 0;
        httpStats = { http_attempts: 0, request_bytes: 0, response_bytes: 0 };
        requestStarted = performance.now();
        const [head, ...messages] = context.messages;
        return { context: { ...context, messages: [
          { ...head, content: response.instructions }, ...messages,
        ] } };
      },
      finishTurn: async ({ message, toolResults }) => {
        // Pi may otherwise retry a truncated tool call; never execute salvaged/incomplete arguments.
        if (message.stopReason === "length") return { action: "end" };
        if (toolResults.length && turn >= config.max_turns) {
          failureCode = "max_turns_exceeded";
          return { action: "end" };
        }
      },
    });
    agent.subscribe(async (event) => {
      if (event.type === "message_update" && requestActive) {
        const delta = event.assistantMessageEvent.delta;
        if (typeof delta === "string") responseBytes += Buffer.byteLength(delta);
        if (responseBytes > MAX_RESPONSE_BYTES) {
          failureCode = "response_too_large";
          agent.abort();
          throw new Error(failureCode);
        }
      }
      if (event.type === "message_end" && event.message.role === "assistant" && requestActive) {
        // Pi can synthesize failure messages when a hook fails; those are not new model requests.
        requestActive = false;
        clearTimeout(requestTimer);
        // Acknowledgement is a persistence barrier before Pi dispatches the next tools.
        await request("model_end", {
          turn, usage: event.message.usage, stop_reason: event.message.stopReason,
          http_status: responseStatus, provider_request_id: providerRequestId,
          latency_ms: Math.round(performance.now() - requestStarted),
          ...httpStats,
          error_category: requestTimedOut ? "timeout" : (
            event.message.stopReason === "error" ? modelErrorCategory(event.message.errorMessage, responseStatus) : null
          ),
        });
        if (requestTimedOut) {
          failureCode = "model_error";
          throw new Error(failureCode);
        }
        const calls = event.message.content.filter((item) => item.type === "toolCall");
        if (!["error", "aborted", "length"].includes(event.message.stopReason)) {
          // Check the complete batch before dispatch: never partially execute an over-budget batch.
          if (toolCalls + calls.length > config.max_tool_calls) {
            failureCode = "max_tool_calls_exceeded";
            throw new Error(failureCode);
          }
          if (calls.some((call) => typeof call.id !== "string" || !call.id ||
              typeof call.name !== "string" || !call.name) || new Set(calls.map((call) => call.id)).size !== calls.length) {
            failureCode = "invalid_tool_calls";
            throw new Error(failureCode);
          }
          toolCalls += calls.length;
        }
      }
    });
    try {
      await agent.prompt(config.prompt);
    } finally {
      clearTimeout(requestTimer);
    }
    const message = agent.state.messages.findLast((item) => item.role === "assistant");
    if (failureCode) throw new Error(failureCode);
    if (!message || message.stopReason === "error" || message.stopReason === "aborted") {
      throw new Error(message?.stopReason === "aborted" ? "aborted" : "model_error");
    }
    if (message.stopReason === "length") throw new Error("output_truncated");
    if (message.stopReason !== "stop") throw new Error("model_error");
    const text = message.content.filter((item) => item.type === "text").map((item) => item.text).join("");
    if (!text.trim()) throw new Error("empty_output");
    await send({ type: "result", text, turns: turn });
  }

  async function complete(config) {
    try {
      await run(config);
    } catch (error) {
      const allowed = new Set(["max_turns_exceeded", "max_tool_calls_exceeded", "invalid_tool_calls",
        "output_truncated", "empty_output", "model_error", "aborted", "response_too_large", "context_too_large"]);
      await send({ type: "error", code: allowed.has(error.message) ? error.message : "worker_error" });
    } finally {
      finished = true;
      abort();
      input.close();
      process.stdin.pause();
    }
  }

  input.on("line", (line) => {
    try {
      if (Buffer.byteLength(line) > 8 * 1024 * 1024) throw new Error("record too large");
      const record = JSON.parse(line);
      if (record.type === "start" && !started) {
        started = true;
        void complete(record);
      } else if (record.type === "response" && pending.has(record.id)) {
        const entry = pending.get(record.id);
        pending.delete(record.id);
        entry.resolve(record.data);
      } else if (record.type === "abort") {
        abort();
      } else {
        abort();
        process.exitCode = 1;
        input.close();
      }
    } catch {
      abort();
      process.exitCode = 1;
      input.close();
    }
  });
  input.on("close", () => { if (!finished) abort(); });
  process.on("SIGTERM", abort);
  process.on("SIGINT", abort);
}

// Only fixed labels cross the bridge; provider error strings can contain credentials/business data.
function modelErrorCategory(message = "", status) {
  if (status === 429) return "rate_limit";
  if (status === 401 || status === 403) return "authentication";
  if (status >= 500 && status <= 599) return "provider_unavailable";
  if (status === 400 || status === 422) return "invalid_request";
  if (/timeout|timed out/i.test(message)) return "timeout";
  if (/rate.?limit|\b429\b/i.test(message)) return "rate_limit";
  if (/unauthorized|authentication|\b40[13]\b/i.test(message)) return "authentication";
  if (/\b5\d\d\b|internal server|bad gateway|service unavailable/i.test(message)) return "provider_unavailable";
  if (/\b400\b|invalid request|bad request/i.test(message)) return "invalid_request";
  if (/content_filter/i.test(message)) return "content_filter";
  if (/network|fetch failed|connection|socket/i.test(message)) return "network";
  return "unknown";
}
