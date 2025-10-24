from typing import Any
from kubernetes import client
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from common import _cache_invalidate, _cache_set, invalid_response
from validators import NamespaceValidator
from k8s_utils import get_clients, load_kube_config


def register_tools(mcp: FastMCP):
    """Register Kubernetes namespace-related MCP tools with full signatures."""
    tools_dict = {}

    def register(signature: dict):
        """Decorator that registers a tool and stores its signature"""
        def wrapper(func):
            mcp.tool()(func)
            func.__tool_signature__ = signature
            tools_dict[func.__name__] = func
            return func
        return wrapper

    # ---------------- CREATE NAMESPACE ----------------
    @register(signature={'name': 'str'})
    def create_namespace(name: str) -> Any:
        """Create a new Kubernetes namespace."""
        if not name or not str(name).strip():
            return invalid_response("Namespace name is required.")

        load_kube_config()
        v1, _, _ = get_clients()
        body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name))

        try:
            v1.create_namespace(body=body)
            _cache_invalidate("namespaces")
            return {"status": "success", "message": f"Namespace '{name}' created successfully."}
        except ApiException as e:
            if getattr(e, "status", None) == 409:
                return {"status": "exists", "message": f"Namespace '{name}' already exists."}
            return {"status": "error", "message": str(e)}

    # ---------------- DELETE NAMESPACE ----------------
    @register(signature={'namespace': 'str'})
    def delete_namespace(namespace: str) -> Any:
        """Delete an existing Kubernetes namespace."""
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        load_kube_config()
        v1, _, _ = get_clients()
        try:
            v1.delete_namespace(name=namespace)
            _cache_invalidate("namespaces")
            _cache_invalidate("deployments::")
            _cache_invalidate("pods::")
            _cache_invalidate("services::")
            return {"status": "success", "message": f"Namespace '{namespace}' deleted successfully."}
        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return invalid_response(f"Namespace '{namespace}' not found.")
            return {"status": "error", "message": str(e)}

    # ---------------- LIST NAMESPACES ----------------
    @register(signature={})
    def list_namespaces() -> Any:
        """List all namespaces with status and creation time."""
        load_kube_config()
        v1, _, _ = get_clients()
        try:
            namespaces = v1.list_namespace()
            result = []
            for ns in namespaces.items:
                result.append({
                    "name": ns.metadata.name,
                    "status": ns.status.phase,
                    "created_at": ns.metadata.creation_timestamp.isoformat() if ns.metadata.creation_timestamp else "N/A"
                })
            # Update cache for fast access
            _cache_set("namespaces", [r["name"] for r in result])
            return result
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return tools_dict
