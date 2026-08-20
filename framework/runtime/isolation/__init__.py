"""Process-isolation package: exposes ``main``, the entry point that runs a
Python agent inside a subprocess.
"""

from framework.runtime.isolation.subprocess_runner import main

__all__ = ["main"]
