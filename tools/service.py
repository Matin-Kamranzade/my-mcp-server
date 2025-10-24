from typing import Any, List
from kubernetes import client
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from common import _cache_invalidate, list_services_cached, invalid_response
from validators import NamespaceValidator, ServiceValidator, DeploymentValidator
from k8s_utils import get_clients


def register_tools(mcp: FastMCP):
    """Register Kubernetes service-related MCP tools."""
    tools_dict = {}

    def register(signature: dict):
        """Decorator that registers a tool and stores its signature."""
        def wrapper(func):
            mcp.tool()(func)
            func.__tool_signature__ = signature
            tools_dict[func.__name__] = func
            return func
        return wrapper

    # ---------------- CREATE SERVICE ----------------
    @register(signature={
        "namespace": "str",
        "name": "str",
        "deployment_name": "str",
        "port": "int",
        "target_port": "int",
        "type": "str",
        "node_port": "int"
    })
    def create_service(
        namespace: str,
        name: str,
        deployment_name: str,
        port: int,
        target_port: int = None,
        type: str = "ClusterIP",
        node_port: int = None
    ) -> Any:
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        dep_validator = DeploymentValidator(namespace, deployment_name)
        validation_error = dep_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()

        try:
            port = int(port)
            target_port = int(target_port or port)
        except Exception:
            return invalid_response("Port and target_port must be integers.")

        service_port = client.V1ServicePort(port=port, target_port=target_port)
        if type == "NodePort" and node_port:
            try:
                service_port.node_port = int(node_port)
            except Exception:
                return invalid_response("node_port must be an integer for NodePort services.")

        body = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(name=name),
            spec=client.V1ServiceSpec(
                selector={"app": deployment_name},
                ports=[service_port],
                type=type
            )
        )

        try:
            v1.create_namespaced_service(namespace=namespace, body=body)
            _cache_invalidate(f"services::{namespace}")
            msg = f"Service '{name}' created in '{namespace}' as type '{type}'."
            if type == "NodePort" and node_port:
                msg += f" NodePort: {node_port}"
            return {"status": "success", "message": msg}
        except ApiException as e:
            if getattr(e, "status", None) == 409:
                return {"status": "exists", "message": f"Service '{name}' already exists in '{namespace}'."}
            return {"status": "error", "message": str(e)}

    # ---------------- DELETE SERVICE ----------------
    @register(signature={
        "name": "str",
        "namespace": "str"
    })
    def delete_service(name: str, namespace: str) -> Any:
        svc_validator = ServiceValidator(namespace, name)
        validation_error = svc_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            v1.delete_namespaced_service(name=name, namespace=namespace)
            _cache_invalidate(f"services::{namespace}")
            return {"status": "success", "message": f"Service '{name}' deleted successfully from '{namespace}'."}
        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return invalid_response(
                    f"Service '{name}' not found in '{namespace}'.",
                    list_services_cached(namespace)
                )
            return {"status": "error", "message": str(e)}

    # ---------------- LIST SERVICES ----------------
    @register(signature={
        "namespace": "str"
    })
    def list_services(namespace: str) -> Any:
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            services = v1.list_namespaced_service(namespace=namespace)
            result = []
            for svc in services.items:
                ports = [{"port": p.port, "protocol": p.protocol} for p in (svc.spec.ports or [])]
                external_ips = "N/A"
                if svc.status and getattr(svc.status, "load_balancer", None) and svc.status.load_balancer.ingress:
                    first = svc.status.load_balancer.ingress[0]
                    external_ips = getattr(first, "ip", getattr(first, "hostname", "N/A"))
                result.append({
                    "name": svc.metadata.name,
                    "type": svc.spec.type,
                    "cluster_ip": svc.spec.cluster_ip,
                    "external_ip": external_ips,
                    "ports": ports
                })
            return result
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    # ---------------- LIST SERVICE ENDPOINTS ----------------
    @register(signature={
        "namespace": "str"
    })
    def list_service_endpoints(namespace: str) -> Any:
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            endpoints = v1.list_namespaced_endpoints(namespace=namespace)
            result = []
            for ep in endpoints.items:
                addresses = []
                for subset in ep.subsets or []:
                    for addr in subset.addresses or []:
                        addresses.append({
                            "ip": addr.ip,
                            "target": addr.target_ref.name if addr.target_ref else "N/A"
                        })
                result.append({
                    "name": ep.metadata.name,
                    "addresses": addresses
                })
            return result
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    return tools_dict
