# Example Agents

This directory contains only reusable, non-domain-specific examples. Production agents should live in a separate package that depends on Agent Task Platform, never the reverse.

Start with the offline implementation in `example_tool_agent/`, then follow [Build an Agent or Tool](../docs/guides/build-an-extension.md). The Pi example in `pi_multistep_demo/` demonstrates a model-directed loop and requires the setup in [Pi Agent Runtime](../docs/guides/pi-runtime.md).
