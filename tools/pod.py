from typing import Any
from datetime import datetime, timezone
from kubernetes import client
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from common import _cache_invalidate, list_pods_cached, invalid_response
from validators import NamespaceValidator, PodValidator
from k8s_utils import get_clients, load_kube_config


def register_tools(mcp: FastMCP):
    """Register Kubernetes pod-related MCP tools with full signatures."""
    tools_dict = {}

    def register(signature: dict):
        """Decorator that registers a tool and stores its signature"""
        def wrapper(func):
            mcp.tool()(func)
            func.__tool_signature__ = signature
            tools_dict[func.__name__] = func
            return func
        return wrapper

    # ---------------- LIST PODS ----------------
    @register(signature={'namespace': 'str'})
    def list_pods(namespace: str) -> Any:
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            pods = v1.list_namespaced_pod(namespace=namespace)
            return [
                {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "status": pod.status.phase,
                    "ip": pod.status.pod_ip,
                    "node": pod.spec.node_name,
                    "created_at": (
                        pod.metadata.creation_timestamp.isoformat()
                        if pod.metadata.creation_timestamp
                        else None
                    ),
                }
                for pod in pods.items
            ]
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    # ---------------- LIST PODS WITH LOGS ----------------
    @register(signature={'namespace': 'str'})
    def list_pods_with_logs(namespace: str) -> Any:
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            pods = v1.list_namespaced_pod(namespace=namespace, watch=False)
            result = []
            for pod in pods.items:
                pod_info = {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "ip": pod.status.pod_ip,
                    "status": pod.status.phase,
                    "node": pod.spec.node_name,
                    "containers": [],
                }

                for container in pod.spec.containers:
                    try:
                        logs = v1.read_namespaced_pod_log(
                            name=pod.metadata.name,
                            namespace=namespace,
                            container=container.name,
                            tail_lines=5,
                        )
                        pod_info["containers"].append({
                            "name": container.name,
                            "logs": logs,
                        })
                    except Exception as e:
                        pod_info["containers"].append({
                            "name": container.name,
                            "error": str(e),
                        })
                result.append(pod_info)
            return result
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    # ---------------- DELETE POD ----------------
    @register(signature={'name': 'str', 'namespace': 'str'})
    def delete_pod(name: str, namespace: str) -> Any:
        pod_validator = PodValidator(namespace, name)
        validation_error = pod_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            v1.delete_namespaced_pod(name=name, namespace=namespace)
            _cache_invalidate(f"pods::{namespace}")
            return {"status": "success", "message": f"Pod '{name}' deleted successfully from '{namespace}'."}
        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return invalid_response(
                    f"Pod '{name}' not found in '{namespace}'.",
                    list_pods_cached(namespace),
                )
            return {"status": "error", "message": str(e)}

    # ---------------- RESTART POD ----------------
    @register(signature={'pod_name': 'str', 'namespace': 'str'})
    def restart_pod(pod_name: str, namespace: str) -> Any:
        """Deletes the pod so it restarts automatically if managed by a controller."""
        pod_validator = PodValidator(namespace, pod_name)
        validation_error = pod_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            owner_refs = pod.metadata.owner_references or []

            v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
            _cache_invalidate(f"pods::{namespace}")

            if owner_refs:
                return {
                    "status": "success",
                    "message": f"Pod '{pod_name}' deleted for restart; controller will recreate it."
                }
            else:
                return {
                    "status": "warning",
                    "message": f"Pod '{pod_name}' deleted manually; not managed by controller, must recreate manually."
                }
        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return invalid_response(
                    f"Pod '{pod_name}' not found in '{namespace}'.",
                    list_pods_cached(namespace),
                )
            return {"status": "error", "message": str(e)}

    return tools_dict
