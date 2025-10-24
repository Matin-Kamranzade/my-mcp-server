from typing import Any
from datetime import datetime, timezone
from kubernetes import client
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from common import _cache_invalidate, list_deployments_cached, invalid_response
from validators import DeploymentValidator, NamespaceValidator
from k8s_utils import get_clients, load_kube_config

def register_tools(mcp: FastMCP):
    """Register Kubernetes deployment-related MCP tools with full signatures."""
    tools_dict = {}

    def register(signature: dict):
        """Decorator that registers a tool and stores its signature"""
        def wrapper(func):
            mcp.tool()(func)              
            func.__tool_signature__ = signature
            tools_dict[func.__name__] = func
            return func
        return wrapper

    # ---------------- CREATE DEPLOYMENT ----------------
    @register(signature={
        'namespace': 'str', 'name': 'str', 'image': 'str', 'replicas': 'int', 'port': 'int'
    })
    def create_deployment(namespace: str, name: str, image: str, replicas: int = 1, port: int = 80) -> Any:
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        load_kube_config()
        apps_v1 = client.AppsV1Api()
        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(name=name),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                selector={'matchLabels': {'app': name}},
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={'app': name}),
                    spec=client.V1PodSpec(
                        containers=[client.V1Container(
                            name=name,
                            image=image,
                            ports=[client.V1ContainerPort(container_port=port)]
                        )]
                    )
                )
            )
        )

        try:
            apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment)
            _cache_invalidate(f"deployments::{namespace}")
            return {"status": "success", "message": f"Deployment '{name}' created in '{namespace}'."}
        except ApiException as e:
            if getattr(e, "status", None) == 409:
                return {"status": "exists", "message": f"Deployment '{name}' already exists in '{namespace}'."}
            return {"status": "error", "message": str(e)}

    # ---------------- DELETE DEPLOYMENT ----------------
    @register(signature={
        'name': 'str', 'namespace': 'str'
    })
    def delete_deployment(name: str = None, namespace: str = None, deployment_name: str = None) -> Any:
        name = name or deployment_name
        if not name:
            return {"status": "error", "message": "Missing deployment name."}

        dep_validator = DeploymentValidator(namespace, name)
        validation_error = dep_validator.validate()
        if validation_error:
            return validation_error

        _, apps_v1, _ = get_clients()
        try:
            apps_v1.delete_namespaced_deployment(name=name, namespace=namespace)
            _cache_invalidate(f"deployments::{namespace}")
            _cache_invalidate(f"pods::{namespace}")
            return {"status": "success", "message": f"Deployment '{name}' deleted successfully from '{namespace}'."}
        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return invalid_response(
                    f"Deployment '{name}' not found in namespace '{namespace}'.",
                    list_deployments_cached(namespace)
                )
            return {"status": "error", "message": str(e)}

    # ---------------- LIST DEPLOYMENTS ----------------
    @register(signature={
        'namespace': 'str'
    })
    def list_deployments(namespace: str) -> Any:
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        _, apps_v1, _ = get_clients()
        try:
            deps = apps_v1.list_namespaced_deployment(namespace=namespace)
            return [
                {
                    "name": dep.metadata.name,
                    "replicas": dep.status.replicas or 0,
                    "available": dep.status.available_replicas or 0,
                    "images": [c.image for c in dep.spec.template.spec.containers],
                    "namespace": dep.metadata.namespace
                }
                for dep in deps.items
            ]
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    # ---------------- RESTART DEPLOYMENT ----------------
    @register(signature={
        'deployment_name': 'str', 'namespace': 'str'
    })
    def restart_deployment(deployment_name: str, namespace: str) -> Any:
        dep_validator = DeploymentValidator(namespace, deployment_name)
        validation_error = dep_validator.validate()
        if validation_error:
            return validation_error

        _, apps_v1, _ = get_clients()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()}}}}}
        try:
            apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=body)
            _cache_invalidate(f"deployments::{namespace}")
            _cache_invalidate(f"pods::{namespace}")
            return {"status": "success", "message": f"Deployment '{deployment_name}' restarted successfully in '{namespace}'."}
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    # ---------------- SCALE DEPLOYMENT ----------------
    @register(signature={
        'deployment_name': 'str', 'replicas': 'int', 'namespace': 'str'
    })
    def scale_deployment(deployment_name: str, replicas: int, namespace: str) -> Any:
        dep_validator = DeploymentValidator(namespace, deployment_name)
        validation_error = dep_validator.validate()
        if validation_error:
            return validation_error

        try:
            replicas = int(replicas)
            if replicas < 0:
                return invalid_response("Replicas must be >= 0")
        except Exception:
            return invalid_response("Replicas must be an integer")

        _, apps_v1, _ = get_clients()
        body = {"spec": {"replicas": replicas}}
        try:
            apps_v1.patch_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=body)
            _cache_invalidate(f"deployments::{namespace}")
            return {"status": "success", "message": f"Deployment '{deployment_name}' scaled to {replicas} in '{namespace}'."}
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    return tools_dict
