-- agent-task-platform registry seed (PostgreSQL)
-- AUTO-GENERATED from config/agents.yaml + config/tools.yaml + config/skills.yaml via scripts/gen_sql.py.
-- Idempotent: re-running upserts by (name, version).

INSERT INTO ai_agent_config (name, version, description, max_concurrency, timeout_seconds, enabled, owner, managed_by, updated_by, last_time, route_tags, input_schema, output_schema, tools, skills, retry_policy, permissions, runtime, mcp_servers) VALUES
  ('echo-agent', '1.0.0', 'Return the submitted input without external dependencies.', 1, 300, TRUE, NULL, 'yaml', NULL, now(), $json$["example.echo"]$json$::jsonb, $json${"type": "object"}$json$::jsonb, $json${"type": "object"}$json$::jsonb, $json$[]$json$::jsonb, $json$[{"name": "example-compliance", "version": "1.0.0", "activation": "always", "config": {}}]$json$::jsonb, $json${"max_attempts": 1, "backoff_seconds": 0.0, "backoff_type": "fixed"}$json$::jsonb, $json$[]$json$::jsonb, $json${"type": "echo"}$json$::jsonb, $json$[]$json$::jsonb)
ON CONFLICT (name, version) DO UPDATE SET
  description = EXCLUDED.description,
  max_concurrency = EXCLUDED.max_concurrency,
  timeout_seconds = EXCLUDED.timeout_seconds,
  enabled = EXCLUDED.enabled,
  owner = EXCLUDED.owner,
  managed_by = EXCLUDED.managed_by,
  updated_by = EXCLUDED.updated_by,
  last_time = EXCLUDED.last_time,
  route_tags = EXCLUDED.route_tags,
  input_schema = EXCLUDED.input_schema,
  output_schema = EXCLUDED.output_schema,
  tools = EXCLUDED.tools,
  skills = EXCLUDED.skills,
  retry_policy = EXCLUDED.retry_policy,
  permissions = EXCLUDED.permissions,
  runtime = EXCLUDED.runtime,
  mcp_servers = EXCLUDED.mcp_servers;

INSERT INTO ai_agent_config (name, version, description, max_concurrency, timeout_seconds, enabled, owner, managed_by, updated_by, last_time, route_tags, input_schema, output_schema, tools, skills, retry_policy, permissions, runtime, mcp_servers) VALUES
  ('calculator-agent', '1.0.0', 'Demonstrate governed local tool calls.', 1, 300, TRUE, NULL, 'yaml', NULL, now(), $json$["example.calculator"]$json$::jsonb, $json${"type": "object"}$json$::jsonb, $json${"type": "object"}$json$::jsonb, $json$["example-weather", "example-calculator"]$json$::jsonb, $json$[]$json$::jsonb, $json${"max_attempts": 1, "backoff_seconds": 0.0, "backoff_type": "fixed"}$json$::jsonb, $json$["tool:example-weather", "tool:example-calculator"]$json$::jsonb, $json${"type": "python", "target": "agent_hub.example_tool_agent:create_agent"}$json$::jsonb, $json$[]$json$::jsonb)
ON CONFLICT (name, version) DO UPDATE SET
  description = EXCLUDED.description,
  max_concurrency = EXCLUDED.max_concurrency,
  timeout_seconds = EXCLUDED.timeout_seconds,
  enabled = EXCLUDED.enabled,
  owner = EXCLUDED.owner,
  managed_by = EXCLUDED.managed_by,
  updated_by = EXCLUDED.updated_by,
  last_time = EXCLUDED.last_time,
  route_tags = EXCLUDED.route_tags,
  input_schema = EXCLUDED.input_schema,
  output_schema = EXCLUDED.output_schema,
  tools = EXCLUDED.tools,
  skills = EXCLUDED.skills,
  retry_policy = EXCLUDED.retry_policy,
  permissions = EXCLUDED.permissions,
  runtime = EXCLUDED.runtime,
  mcp_servers = EXCLUDED.mcp_servers;

INSERT INTO ai_tool_config (name, version, description, timeout_seconds, max_concurrency, qps, enabled, owner, managed_by, updated_by, last_time, input_schema, output_schema, endpoint, retry_policy, operation_type, idempotency_key_header, allowed_agents, circuit_breaker) VALUES
  ('example-weather', '1.0.0', 'Return deterministic sample weather data.', 30, 10, NULL, TRUE, NULL, 'yaml', NULL, now(), $json${"type": "object", "properties": {"city": {"type": "string"}, "date": {"type": ["string", "null"]}}, "required": ["city"], "additionalProperties": false}$json$::jsonb, $json${"type": "object"}$json$::jsonb, $json${"protocol": "python", "target": "plugins.tools.weather:get_weather"}$json$::jsonb, $json${"max_attempts": 1, "backoff_seconds": 0.0, "backoff_type": "fixed"}$json$::jsonb, 'read_only', NULL, $json$["calculator-agent"]$json$::jsonb, $json${}$json$::jsonb)
ON CONFLICT (name, version) DO UPDATE SET
  description = EXCLUDED.description,
  timeout_seconds = EXCLUDED.timeout_seconds,
  max_concurrency = EXCLUDED.max_concurrency,
  qps = EXCLUDED.qps,
  enabled = EXCLUDED.enabled,
  owner = EXCLUDED.owner,
  managed_by = EXCLUDED.managed_by,
  updated_by = EXCLUDED.updated_by,
  last_time = EXCLUDED.last_time,
  input_schema = EXCLUDED.input_schema,
  output_schema = EXCLUDED.output_schema,
  endpoint = EXCLUDED.endpoint,
  retry_policy = EXCLUDED.retry_policy,
  operation_type = EXCLUDED.operation_type,
  idempotency_key_header = EXCLUDED.idempotency_key_header,
  allowed_agents = EXCLUDED.allowed_agents,
  circuit_breaker = EXCLUDED.circuit_breaker;

INSERT INTO ai_tool_config (name, version, description, timeout_seconds, max_concurrency, qps, enabled, owner, managed_by, updated_by, last_time, input_schema, output_schema, endpoint, retry_policy, operation_type, idempotency_key_header, allowed_agents, circuit_breaker) VALUES
  ('example-calculator', '1.0.0', 'Evaluate a simple arithmetic expression.', 30, 10, NULL, TRUE, NULL, 'yaml', NULL, now(), $json${"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"], "additionalProperties": false}$json$::jsonb, $json${"type": "object"}$json$::jsonb, $json${"protocol": "python", "target": "plugins.tools.calculator:calculate"}$json$::jsonb, $json${"max_attempts": 1, "backoff_seconds": 0.0, "backoff_type": "fixed"}$json$::jsonb, 'read_only', NULL, $json$["calculator-agent"]$json$::jsonb, $json${}$json$::jsonb)
ON CONFLICT (name, version) DO UPDATE SET
  description = EXCLUDED.description,
  timeout_seconds = EXCLUDED.timeout_seconds,
  max_concurrency = EXCLUDED.max_concurrency,
  qps = EXCLUDED.qps,
  enabled = EXCLUDED.enabled,
  owner = EXCLUDED.owner,
  managed_by = EXCLUDED.managed_by,
  updated_by = EXCLUDED.updated_by,
  last_time = EXCLUDED.last_time,
  input_schema = EXCLUDED.input_schema,
  output_schema = EXCLUDED.output_schema,
  endpoint = EXCLUDED.endpoint,
  retry_policy = EXCLUDED.retry_policy,
  operation_type = EXCLUDED.operation_type,
  idempotency_key_header = EXCLUDED.idempotency_key_header,
  allowed_agents = EXCLUDED.allowed_agents,
  circuit_breaker = EXCLUDED.circuit_breaker;

INSERT INTO ai_skill_config (name, version, description, source_path, content_hash, enabled, compatibility, owner, managed_by, updated_by, last_time, allowed_tools, scripts, artifact, metadata) VALUES
  ('example-compliance', '1.0.0', 'Classify synthetic example inputs using a small, reusable review workflow.', 'example-compliance', '0ad5a2ca987e3164f960916aee150510c0c17898ab6da0997b5628a19b78b6a7', TRUE, NULL, 'example-maintainers', 'yaml', NULL, now(), $json$[]$json$::jsonb, $json$[]$json$::jsonb, $json$[{"path": "SKILL.md", "content_base64": "LS0tCm5hbWU6IGV4YW1wbGUtY29tcGxpYW5jZQpkZXNjcmlwdGlvbjogQ2xhc3NpZnkgc3ludGhldGljIGV4YW1wbGUgaW5wdXRzIHVzaW5nIGEgc21hbGwsIHJldXNhYmxlIHJldmlldyB3b3JrZmxvdy4KbWV0YWRhdGE6CiAgdmVyc2lvbjogIjEuMC4wIgotLS0KCiMgRXhhbXBsZSBSZXZpZXcgV29ya2Zsb3cKClVzZSB0aGlzIFNraWxsIG9ubHkgZm9yIHN5bnRoZXRpYyBleGFtcGxlIGlucHV0cy4KCjEuIFJlYWQgYHJlZmVyZW5jZXMvcmlzay1sZXZlbHMubWRgIGJlZm9yZSBjaG9vc2luZyBhIGNsYXNzaWZpY2F0aW9uLgoyLiBDbGFzc2lmeSB0aGUgaW5wdXQgYXMgbG93LCBtZWRpdW0sIG9yIGhpZ2guCjMuIFJldHVybiB0aGUgY2xhc3NpZmljYXRpb24gYW5kIGEgc2hvcnQgZXhwbGFuYXRpb24gYmFzZWQgb24gdGhlIHJlZmVyZW5jZS4KClRoZSByZWZlcmVuY2UgaXMgc3VwcG9ydGluZyBpbmZvcm1hdGlvbiwgbm90IGF1dGhvcml0YXRpdmUgcG9saWN5Lgo=", "mime_type": "text/markdown"}, {"path": "references/risk-levels.md", "content_base64": "IyBTeW50aGV0aWMgUmlzayBMZXZlbHMKCi0gTG93OiBubyBtYXRlcmlhbCBzeW50aGV0aWMgcmlzayBpbmRpY2F0b3JzIGFyZSBwcmVzZW50LgotIE1lZGl1bTogdGhlIGlucHV0IGhhcyBpbmRpY2F0b3JzIHRoYXQgbmVlZCBhZGRpdGlvbmFsIGV4YW1wbGUgcmV2aWV3LgotIEhpZ2g6IHRoZSBpbnB1dCBoYXMgaW5kaWNhdG9ycyB0aGF0IHNob3VsZCBiZSBlc2NhbGF0ZWQgaW4gdGhlIGV4YW1wbGUgd29ya2Zsb3cuCg==", "mime_type": "text/markdown"}]$json$::jsonb, $json${"version": "1.0.0"}$json$::jsonb)
ON CONFLICT (name, version) DO UPDATE SET
  description = EXCLUDED.description,
  source_path = EXCLUDED.source_path,
  content_hash = EXCLUDED.content_hash,
  enabled = EXCLUDED.enabled,
  compatibility = EXCLUDED.compatibility,
  owner = EXCLUDED.owner,
  managed_by = EXCLUDED.managed_by,
  updated_by = EXCLUDED.updated_by,
  last_time = EXCLUDED.last_time,
  allowed_tools = EXCLUDED.allowed_tools,
  scripts = EXCLUDED.scripts,
  artifact = EXCLUDED.artifact,
  metadata = EXCLUDED.metadata;
