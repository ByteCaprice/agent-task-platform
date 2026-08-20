"""``PythonAgentLoader``: loads an in-process agent from a ``module:factory``
target string and calls the factory to build the agent instance.
"""

from __future__ import annotations

import importlib

from framework.runtime.context import Agent


class PythonAgentLoader:
    @staticmethod
    def load(target: str) -> Agent:
        module_name, factory_name = target.split(":", 1)
        factory = getattr(importlib.import_module(module_name), factory_name)
        return factory()
