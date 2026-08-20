"""Agent runtime package: exports ``AgentRuntime`` plus the built-in agent
adapters (echo, http, model_gateway, openai_agents, python, subprocess).
"""

from framework.runtime.adapters.echo import EchoAgent, FailingAgent
from framework.runtime.adapters.http import HTTPAgent
from framework.runtime.adapters.model_gateway_adapter import ModelGatewayAgent
from framework.runtime.adapters.openai_agents import OpenAIAgentsSDKAgent
from framework.runtime.adapters.python import PythonAgentLoader
from framework.runtime.adapters.subprocess_python import SubprocessPythonAgent
from framework.runtime.agent_runtime import AgentRuntime
from framework.runtime.context import Agent, AgentContext
from framework.runtime.errors import RunCancelledError, StageStateError
from framework.runtime.stage_runner import StageRunner
from framework.runtime.state import AgentStateClient, RuntimeStateClient
from framework.tool.stage_context import StageExecutionContext

__all__ = [
    "Agent",
    "AgentContext",
    "AgentRuntime",
    "AgentStateClient",
    "RunCancelledError",
    "StageRunner",
    "StageExecutionContext",
    "StageStateError",
    "RuntimeStateClient",
    "EchoAgent",
    "FailingAgent",
    "HTTPAgent",
    "ModelGatewayAgent",
    "OpenAIAgentsSDKAgent",
    "PythonAgentLoader",
    "SubprocessPythonAgent",
]
