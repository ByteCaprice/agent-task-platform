"""Agent Hub — business agent and workflow packages.

Each agent lives in its own sub-directory as a Python package containing:

    my_agent/
        __init__.py       # exports create_agent() factory
        agent.py          # Agent class implementing the Agent Protocol
        prompts.py        # (optional) prompt templates

Agents are loaded via ``runtime.type: python`` with ``target: agent_hub.my_agent:create_agent``.
"""
