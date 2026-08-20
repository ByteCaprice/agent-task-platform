"""Tool Hub — directory of reusable tool implementations.

Each tool is a single .py file using the @function_tool decorator from framework.tool.function_tool.
Tools are auto-discovered and can be registered via YAML config with ``protocol: python``.

Example YAML registration::

    tools:
      - name: example-weather
        endpoint:
          protocol: python
          target: plugins.tools.weather:get_weather       # module:function reference

The decorated functions:
- Auto-generate JSON Schema from Python type hints
- Support failure_error_function for graceful error handling
- Can be used directly by agents or through ToolGateway
"""

from __future__ import annotations
