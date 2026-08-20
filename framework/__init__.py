"""Platform kernel: agent runtime, tool runtime, model gateway, plugin registry.

Defines *how* to run agents/tools; never imports concrete business agents or
tools (discovers them via the registry / config).
"""
