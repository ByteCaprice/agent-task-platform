-- agent-task-platform schema (PostgreSQL)
-- AUTO-GENERATED from the ORM entities via scripts/gen_sql.py. Do not hand-edit;
-- regenerate after schema changes. Source of truth: store/model/ + Alembic.

-- ===== ai_agent_config =====
CREATE TABLE IF NOT EXISTS ai_agent_config (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	version TEXT NOT NULL,
	description TEXT,
	max_concurrency INTEGER NOT NULL,
	timeout_seconds INTEGER NOT NULL,
	enabled BOOLEAN NOT NULL,
	owner TEXT,
	managed_by TEXT NOT NULL,
	updated_by TEXT,
	last_time TIMESTAMP WITH TIME ZONE NOT NULL,
	route_tags JSONB NOT NULL,
	input_schema JSONB NOT NULL,
	output_schema JSONB NOT NULL,
	tools JSONB NOT NULL,
	skills JSONB NOT NULL,
	retry_policy JSONB NOT NULL,
	permissions JSONB NOT NULL,
	runtime JSONB NOT NULL,
	mcp_servers JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_agent_config UNIQUE (name, version)
);

-- ===== ai_agent_run =====
CREATE TABLE IF NOT EXISTS ai_agent_run (
	id TEXT NOT NULL,
	conversation_id TEXT,
	trace_id TEXT NOT NULL,
	caller TEXT NOT NULL,
	route_tag TEXT NOT NULL,
	request_id TEXT NOT NULL,
	status TEXT NOT NULL,
	priority INTEGER NOT NULL,
	current_step TEXT,
	attempts INTEGER NOT NULL,
	max_attempts INTEGER NOT NULL,
	dead_letter_reason TEXT,
	worker TEXT,
	lease_expire_time TIMESTAMP WITH TIME ZONE,
	agent_name TEXT,
	agent_version TEXT,
	error_type TEXT,
	error_message TEXT,
	callback_status TEXT,
	callback_event_id TEXT,
	timeout_seconds INTEGER,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	update_time TIMESTAMP WITH TIME ZONE NOT NULL,
	queue_time TIMESTAMP WITH TIME ZONE,
	run_after TIMESTAMP WITH TIME ZONE,
	start_time TIMESTAMP WITH TIME ZONE,
	finish_time TIMESTAMP WITH TIME ZONE,
	input JSONB NOT NULL,
	output JSONB,
	metadata JSONB NOT NULL,
	files JSONB NOT NULL,
	callback JSONB,
	skill_snapshots JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_agent_run_request UNIQUE (caller, route_tag, request_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_agent_run_agent ON ai_agent_run (agent_name, status);
CREATE INDEX IF NOT EXISTS idx_ai_agent_run_callback_status ON ai_agent_run (callback_status);
CREATE INDEX IF NOT EXISTS idx_ai_agent_run_conversation ON ai_agent_run (conversation_id);
CREATE INDEX IF NOT EXISTS idx_ai_agent_run_finished ON ai_agent_run (finish_time);
CREATE INDEX IF NOT EXISTS idx_ai_agent_run_request ON ai_agent_run (caller, request_id);
CREATE INDEX IF NOT EXISTS idx_ai_agent_run_status ON ai_agent_run (status);
CREATE INDEX IF NOT EXISTS idx_ai_agent_run_trace ON ai_agent_run (trace_id);

-- ===== ai_agent_stage =====
CREATE TABLE IF NOT EXISTS ai_agent_stage (
	id TEXT NOT NULL,
	run_id TEXT NOT NULL,
	trace_id TEXT NOT NULL,
	agent_name TEXT NOT NULL,
	agent_version TEXT NOT NULL,
	stage_key TEXT NOT NULL,
	stage_index INTEGER NOT NULL,
	schema_version TEXT NOT NULL,
	definition_version TEXT NOT NULL,
	status TEXT NOT NULL,
	run_attempt INTEGER NOT NULL,
	attempts INTEGER NOT NULL,
	max_attempts INTEGER NOT NULL,
	execution_id TEXT,
	idempotency_key TEXT NOT NULL,
	input_hash TEXT NOT NULL,
	output JSONB,
	checkpoint JSONB,
	error_type TEXT,
	error_message TEXT,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	update_time TIMESTAMP WITH TIME ZONE NOT NULL,
	start_time TIMESTAMP WITH TIME ZONE,
	finish_time TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_agent_stage_run_key UNIQUE (run_id, stage_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_agent_stage_run ON ai_agent_stage (run_id, stage_index);
CREATE INDEX IF NOT EXISTS idx_ai_agent_stage_status ON ai_agent_stage (status);

-- ===== ai_api_key_config =====
CREATE TABLE IF NOT EXISTS ai_api_key_config (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	api_key TEXT NOT NULL,
	scopes JSONB NOT NULL,
	enabled BOOLEAN NOT NULL,
	last_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_api_key_config_name UNIQUE (name),
	CONSTRAINT uk_ai_api_key_config_key UNIQUE (api_key),
	CONSTRAINT ck_ai_api_key_name CHECK (length(name) > 0),
	CONSTRAINT ck_ai_api_key_value CHECK (length(api_key) > 0),
	CONSTRAINT ck_ai_api_key_scopes_array CHECK (jsonb_typeof(scopes) = 'array'),
	CONSTRAINT ck_ai_api_key_scopes_nonempty CHECK (jsonb_array_length(scopes) > 0)
);

-- ===== ai_callback_config =====
CREATE TABLE IF NOT EXISTS ai_callback_config (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	timeout_seconds FLOAT NOT NULL,
	max_attempts INTEGER NOT NULL,
	backoff_seconds FLOAT NOT NULL,
	backoff_type TEXT NOT NULL,
	signing_secret TEXT,
	url_allowlist JSONB NOT NULL,
	channels JSONB NOT NULL,
	dispatch_poll_interval_seconds FLOAT NOT NULL,
	dispatch_batch_size INTEGER NOT NULL,
	last_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_callback_config_name UNIQUE (name),
	CONSTRAINT ck_ai_callback_timeout CHECK (timeout_seconds > 0),
	CONSTRAINT ck_ai_callback_attempts CHECK (max_attempts > 0),
	CONSTRAINT ck_ai_callback_backoff CHECK (backoff_seconds >= 0),
	CONSTRAINT ck_ai_callback_backoff_type CHECK (backoff_type IN ('exponential','fixed')),
	CONSTRAINT ck_ai_callback_allowlist_array CHECK (jsonb_typeof(url_allowlist) = 'array'),
	CONSTRAINT ck_ai_callback_channels_object CHECK (jsonb_typeof(channels) = 'object'),
	CONSTRAINT ck_ai_callback_dispatch_poll CHECK (dispatch_poll_interval_seconds > 0),
	CONSTRAINT ck_ai_callback_dispatch_batch CHECK (dispatch_batch_size > 0)
);

-- ===== ai_callback_log =====
CREATE TABLE IF NOT EXISTS ai_callback_log (
	id TEXT NOT NULL,
	run_id TEXT NOT NULL,
	trace_id TEXT NOT NULL,
	url TEXT,
	status TEXT NOT NULL,
	attempts INTEGER NOT NULL,
	last_error TEXT,
	run_after TIMESTAMP WITH TIME ZONE,
	worker TEXT,
	lease_expire_time TIMESTAMP WITH TIME ZONE,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	update_time TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_ai_callback_log_run ON ai_callback_log (run_id);
CREATE INDEX IF NOT EXISTS idx_ai_callback_log_run_after ON ai_callback_log (run_after);
CREATE INDEX IF NOT EXISTS idx_ai_callback_log_status ON ai_callback_log (status);

-- ===== ai_conversation =====
CREATE TABLE IF NOT EXISTS ai_conversation (
	id TEXT NOT NULL,
	caller TEXT NOT NULL,
	external_id TEXT NOT NULL,
	task_type TEXT,
	source TEXT,
	route_tag TEXT NOT NULL,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	update_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_conversation_external_id UNIQUE (caller, external_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_conversation_external_id ON ai_conversation (caller, external_id);

-- ===== ai_model_call =====
CREATE TABLE IF NOT EXISTS ai_model_call (
	id TEXT NOT NULL,
	run_id TEXT NOT NULL,
	trace_id TEXT NOT NULL,
	agent_name TEXT NOT NULL,
	agent_version TEXT,
	provider TEXT,
	model TEXT,
	prompt_version TEXT,
	prompt_hash TEXT,
	input_summary TEXT,
	output_summary TEXT,
	status TEXT NOT NULL,
	error TEXT,
	prompt_tokens INTEGER,
	completion_tokens INTEGER,
	total_tokens INTEGER,
	estimated_cost FLOAT NOT NULL,
	http_status INTEGER,
	start_time TIMESTAMP WITH TIME ZONE NOT NULL,
	finish_time TIMESTAMP WITH TIME ZONE,
	latency_ms INTEGER,
	error_type TEXT,
	metadata JSONB NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_ai_model_call_agent ON ai_model_call (agent_name);
CREATE INDEX IF NOT EXISTS idx_ai_model_call_run ON ai_model_call (run_id);
CREATE INDEX IF NOT EXISTS idx_ai_model_call_status ON ai_model_call (status);
CREATE INDEX IF NOT EXISTS idx_ai_model_call_trace ON ai_model_call (trace_id);

-- ===== ai_model_config =====
CREATE TABLE IF NOT EXISTS ai_model_config (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	base_url TEXT,
	api_key TEXT,
	model TEXT,
	timeout_seconds FLOAT NOT NULL,
	temperature FLOAT NOT NULL,
	max_retries INTEGER NOT NULL,
	initial_backoff_seconds FLOAT NOT NULL,
	max_backoff_seconds FLOAT NOT NULL,
	fallback_models JSONB NOT NULL,
	last_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_model_config_name UNIQUE (name),
	CONSTRAINT ck_ai_model_timeout CHECK (timeout_seconds > 0),
	CONSTRAINT ck_ai_model_temperature CHECK (temperature >= 0 AND temperature <= 2),
	CONSTRAINT ck_ai_model_retries CHECK (max_retries >= 0),
	CONSTRAINT ck_ai_model_initial_backoff CHECK (initial_backoff_seconds >= 0),
	CONSTRAINT ck_ai_model_max_backoff CHECK (max_backoff_seconds >= 0),
	CONSTRAINT ck_ai_model_backoff_range CHECK (max_backoff_seconds >= initial_backoff_seconds),
	CONSTRAINT ck_ai_model_fallbacks_array CHECK (jsonb_typeof(fallback_models) = 'array')
);

-- ===== ai_run_log =====
CREATE TABLE IF NOT EXISTS ai_run_log (
	id TEXT NOT NULL,
	run_id TEXT,
	trace_id TEXT NOT NULL,
	component TEXT NOT NULL,
	event_type TEXT NOT NULL,
	level TEXT NOT NULL,
	message TEXT,
	create_time TIMESTAMP WITH TIME ZONE NOT NULL,
	data JSONB NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_ai_run_log_trace ON ai_run_log (trace_id);

-- ===== ai_runtime_config =====
CREATE TABLE IF NOT EXISTS ai_runtime_config (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	worker_poll_interval_seconds FLOAT NOT NULL,
	worker_batch_size INTEGER NOT NULL,
	worker_lease_seconds FLOAT NOT NULL,
	worker_shutdown_timeout_seconds FLOAT NOT NULL,
	queue_global_max_concurrency INTEGER NOT NULL,
	queue_route_limits JSONB NOT NULL,
	queue_caller_limits JSONB NOT NULL,
	queue_max_nested_depth INTEGER NOT NULL,
	queue_max_active_children_per_root INTEGER NOT NULL,
	rate_limit_enabled BOOLEAN NOT NULL,
	rate_limit_default_capacity FLOAT NOT NULL,
	rate_limit_default_fill_rate FLOAT NOT NULL,
	rate_limit_caller_limits JSONB NOT NULL,
	config_watcher_poll_interval_seconds FLOAT NOT NULL,
	kanban_require_api_key BOOLEAN NOT NULL,
	logging_level TEXT NOT NULL,
	logging_max_file_size_mb INTEGER NOT NULL,
	logging_retention_days INTEGER NOT NULL,
	logging_max_body_chars INTEGER NOT NULL,
	run_log_retention_days FLOAT,
	callback_retention_days FLOAT,
	model_call_retention_days FLOAT,
	last_time TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_runtime_config_name UNIQUE (name),
	CONSTRAINT ck_ai_runtime_worker_poll CHECK (worker_poll_interval_seconds > 0),
	CONSTRAINT ck_ai_runtime_worker_batch CHECK (worker_batch_size > 0),
	CONSTRAINT ck_ai_runtime_worker_lease CHECK (worker_lease_seconds > 0),
	CONSTRAINT ck_ai_runtime_worker_shutdown CHECK (worker_shutdown_timeout_seconds > 0),
	CONSTRAINT ck_ai_runtime_queue_concurrency CHECK (queue_global_max_concurrency > 0),
	CONSTRAINT ck_ai_runtime_queue_depth CHECK (queue_max_nested_depth >= 0),
	CONSTRAINT ck_ai_runtime_queue_children CHECK (queue_max_active_children_per_root > 0),
	CONSTRAINT ck_ai_runtime_route_limits_object CHECK (jsonb_typeof(queue_route_limits) = 'object'),
	CONSTRAINT ck_ai_runtime_caller_limits_object CHECK (jsonb_typeof(queue_caller_limits) = 'object'),
	CONSTRAINT ck_ai_runtime_rate_capacity CHECK (rate_limit_default_capacity > 0),
	CONSTRAINT ck_ai_runtime_rate_fill CHECK (rate_limit_default_fill_rate >= 0),
	CONSTRAINT ck_ai_runtime_rate_callers_object CHECK (jsonb_typeof(rate_limit_caller_limits) = 'object'),
	CONSTRAINT ck_ai_runtime_watcher_poll CHECK (config_watcher_poll_interval_seconds > 0),
	CONSTRAINT ck_ai_runtime_logging_level CHECK (logging_level IN ('debug','info','warning','warn','error','critical','fatal','notset')),
	CONSTRAINT ck_ai_runtime_log_file_size CHECK (logging_max_file_size_mb > 0),
	CONSTRAINT ck_ai_runtime_log_retention CHECK (logging_retention_days > 0),
	CONSTRAINT ck_ai_runtime_log_body CHECK (logging_max_body_chars > 0),
	CONSTRAINT ck_ai_runtime_run_retention CHECK (run_log_retention_days IS NULL OR run_log_retention_days > 0),
	CONSTRAINT ck_ai_runtime_callback_retention CHECK (callback_retention_days IS NULL OR callback_retention_days > 0),
	CONSTRAINT ck_ai_runtime_model_retention CHECK (model_call_retention_days IS NULL OR model_call_retention_days > 0)
);

-- ===== ai_skill_config =====
CREATE TABLE IF NOT EXISTS ai_skill_config (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	version TEXT NOT NULL,
	description TEXT NOT NULL,
	source_path TEXT NOT NULL,
	content_hash TEXT NOT NULL,
	enabled BOOLEAN NOT NULL,
	compatibility TEXT,
	owner TEXT,
	managed_by TEXT NOT NULL,
	updated_by TEXT,
	last_time TIMESTAMP WITH TIME ZONE NOT NULL,
	allowed_tools JSONB NOT NULL,
	scripts JSONB NOT NULL,
	artifact JSONB NOT NULL,
	metadata JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_skill_config UNIQUE (name, version)
);

-- ===== ai_tool_config =====
CREATE TABLE IF NOT EXISTS ai_tool_config (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	version TEXT NOT NULL,
	description TEXT,
	timeout_seconds FLOAT NOT NULL,
	max_concurrency INTEGER NOT NULL,
	qps FLOAT,
	enabled BOOLEAN NOT NULL,
	owner TEXT,
	managed_by TEXT NOT NULL,
	updated_by TEXT,
	last_time TIMESTAMP WITH TIME ZONE NOT NULL,
	input_schema JSONB NOT NULL,
	output_schema JSONB NOT NULL,
	endpoint JSONB NOT NULL,
	retry_policy JSONB NOT NULL,
	operation_type TEXT DEFAULT 'read_only' NOT NULL,
	idempotency_key_header TEXT,
	allowed_agents JSONB NOT NULL,
	circuit_breaker JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uk_ai_tool_config UNIQUE (name, version)
);
