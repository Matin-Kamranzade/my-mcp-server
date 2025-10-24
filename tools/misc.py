from typing import Any, Dict
from datetime import datetime, timezone
from kubernetes import client, utils
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP

from k8s_utils import get_clients, load_kube_config, get_yaml_dir
from validators import NamespaceValidator, DeploymentValidator
from common import _cache_invalidate, invalid_response


def register_tools(mcp: FastMCP):
    """
    Register miscellaneous Kubernetes-related tools:
    - YAML apply
    - Autoscaler creation
    - Node listing
    - Cluster info
    - Time sync check
    """
    tools_dict = {}

    def register(signature: dict):
        """Decorator to register tools with MCP and store their signatures"""
        def wrapper(func):
            mcp.tool()(func)
            func.__tool_signature__ = signature
            tools_dict[func.__name__] = func
            return func
        return wrapper

    # ---------------- APPLY YAML ----------------
    @register(signature={'yaml_content': 'str', 'yaml_path': 'str', 'filename': 'str'})
    def apply_yaml(yaml_content: str = None, yaml_path: str = None, filename: str = None) -> Any:
        """
        Apply a YAML manifest directly or from a file path.
        Automatically writes yaml_content to a temporary file if provided.
        """
        if not yaml_content and not yaml_path:
            return invalid_response("Either 'yaml_content' or 'yaml_path' must be provided.")

        load_kube_config()
        k8s_client = client.ApiClient()

        if yaml_content:
            yaml_dir = get_yaml_dir()
            filename = filename or "generated.yaml"
            yaml_path = yaml_dir / filename
            with open(yaml_path, "w", encoding="utf-8") as f:
                f.write(yaml_content)

        try:
            utils.create_from_yaml(k8s_client, str(yaml_path))
            _cache_invalidate("deployments::")
            _cache_invalidate("pods::")
            _cache_invalidate("services::")
            _cache_invalidate("namespaces")
            return {"status": "success", "message": f"Applied manifests from {yaml_path}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ---------------- CREATE AUTOSCALER ----------------
    @register(signature={
        'namespace': 'str', 'deployment_name': 'str',
        'min_replicas': 'int', 'max_replicas': 'int', 'cpu_percent': 'int'
    })
    def create_autoscaler(namespace: str, deployment_name: str, min_replicas: int, max_replicas: int, cpu_percent: int):
        """
        Create a HorizontalPodAutoscaler for a specific deployment.
        """
        dep_validator = DeploymentValidator(namespace, deployment_name)
        validation = dep_validator.validate()
        if validation:
            return validation

        _, _, autoscaling_v1 = get_clients()
        body = client.V1HorizontalPodAutoscaler(
            api_version="autoscaling/v1",
            kind="HorizontalPodAutoscaler",
            metadata=client.V1ObjectMeta(name=f"{deployment_name}-autoscaler"),
            spec=client.V1HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V1CrossVersionObjectReference(
                    api_version="apps/v1",
                    kind="Deployment",
                    name=deployment_name
                ),
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                target_cpu_utilization_percentage=cpu_percent
            )
        )

        try:
            autoscaling_v1.create_namespaced_horizontal_pod_autoscaler(namespace=namespace, body=body)
            return {"status": "success", "message": f"Autoscaler for '{deployment_name}' created."}
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    # ---------------- LIST NODES ----------------
    @register(signature={})
    def list_nodes() -> Any:
        """
        Return all cluster nodes with status, IP, OS, and kernel version.
        """
        v1, _, _ = get_clients()
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

    # ---------------- CLUSTER INFO ----------------
    @register(signature={})
    def cluster_info() -> Any:
        """
        Basic info about cluster state: number of nodes, namespaces, deployments.
        """
        v1, apps_v1, _ = get_clients()
        ns_count = len(v1.list_namespace().items)
        node_count = len(v1.list_node().items)
        total_deployments = sum(len(apps_v1.list_namespaced_deployment(ns.metadata.name).items)
                                for ns in v1.list_namespace().items)
        return {
            "namespaces": ns_count,
            "nodes": node_count,
            "deployments": total_deployments,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    # ---------------- SERVER TIME CHECK ----------------
    @register(signature={})
    def server_time() -> Any:
        """
        Returns the current UTC time of the MCP server.
        Useful for debugging clock skew or scheduling issues.
        """
        return {"utc_time": datetime.now(timezone.utc).isoformat()}

    return tools_dict
