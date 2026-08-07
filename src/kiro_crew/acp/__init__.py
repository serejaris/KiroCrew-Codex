# Modified 2026 by Sereja Ris for VibecodersCrew (community fork of Kiro Crew).
# See NOTICE and CHANGELOG.md for the nature of the modifications.
"""ACP package — Agent Client Protocol for kiro-cli.

Public names are loaded lazily. This keeps ``kiro_crew.acp.types`` importable by
the provider abstraction without eagerly importing ``runtime`` back through
``session_pid -> providers.base`` and creating an import cycle.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AcpClient",
    "AcpError",
    "AcpPermissionNeeded",
    "AcpProcessDied",
    "AcpTimeoutError",
    "AcpRuntime",
    "AcpSessionHandle",
    "AcpEvent",
    "AcpPromptStats",
    "JsonRpcMessage",
    "JsonRpcRequest",
]

_EXPORT_MODULE = {
    "AcpClient": "kiro_crew.acp.client",
    "AcpError": "kiro_crew.acp.client",
    "AcpPermissionNeeded": "kiro_crew.acp.client",
    "AcpProcessDied": "kiro_crew.acp.client",
    "AcpTimeoutError": "kiro_crew.acp.client",
    "AcpRuntime": "kiro_crew.acp.runtime",
    "AcpSessionHandle": "kiro_crew.acp.runtime",
    "AcpEvent": "kiro_crew.acp.types",
    "AcpPromptStats": "kiro_crew.acp.types",
    "JsonRpcMessage": "kiro_crew.acp.types",
    "JsonRpcRequest": "kiro_crew.acp.types",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULE.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
