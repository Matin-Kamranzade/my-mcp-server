# tools/nodes_tools.py

from typing import Any, List
from kubernetes import client
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from common import _cache_invalidate, invalid_response
from k8s_utils import get_clients


def register_tools(mcp: FastMCP):
    """Register Kubernetes node-related MCP tools."""

    tools_dict = {}

    def register(signature: dict):
        """Decorator that registers a tool and stores its signature."""
        def wrapper(func):
            mcp.tool()(func)
            func.__tool_signature__ = signature
            tools_dict[func.__name__] = func
            return func
        return wrapper

    # ---------------- LIST NODES ----------------
    @register(signature={})
    def list_nodes() -> Any:
        """
        Return all cluster nodes with status, IP, OS, and kernel version.
        """
        v1, _, _ = get_clients()
        try:
            nodes = v1.list_node()
            result = []
            for node in nodes.items:
                result.append({
                    "name": node.metadata.name,
                    "status": f"{node.status.conditions[-1].type} - {node.status.conditions[-1].status}",
                    "internal_ip": node.status.addresses[0].address if node.status.addresses else 'N/A',
                    "kernel_version": node.status.node_info.kernel_version,
                    "os_image": node.status.node_info.os_image
                })
            return result
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    # ---------------- NODES WITH PROBLEMS ----------------
    @register(signature={})
    def get_nodes_with_problems() -> Any:
        """
        Returns nodes that are not in 'Ready' state, including reason and message.
        """
        v1, _, _ = get_clients()
        try:
            bad_nodes = []
            nodes = v1.list_node()
            for node in nodes.items:
                for cond in node.status.conditions:
                    if cond.type == "Ready" and cond.status != "True":
                        bad_nodes.append({
                            "name": node.metadata.name,
                            "condition": cond.type,
                            "status": cond.status,
                            "reason": cond.reason,
                            "message": cond.message,
                        })
            return {"nodes_with_problems": bad_nodes}
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    return tools_dict
