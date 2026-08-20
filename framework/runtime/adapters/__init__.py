"""Built-in agent adapters: each wraps one runtime ``type`` (echo, http,
model_gateway, openai_agents, python, subprocess) behind the ``Agent`` protocol.
"""

from framework.runtime.adapters.echo import EchoAgent, FailingAgent
from framework.runtime.adapters.http import HTTPAgent
from framework.runtime.adapters.model_gateway_adapter import ModelGatewayAgent
from framework.runtime.adapters.openai_agents import OpenAIAgentsSDKAgent
from framework.runtime.adapters.python import PythonAgentLoader
from framework.runtime.adapters.subprocess_python import SubprocessPythonAgent

__all__ = [
    "EchoAgent",
    "FailingAgent",
    "HTTPAgent",
    "ModelGatewayAgent",
    "OpenAIAgentsSDKAgent",
    "PythonAgentLoader",
    "SubprocessPythonAgent",
]
