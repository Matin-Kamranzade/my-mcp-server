import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from kubernetes import client, config, utils
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP

# Initialize MCP
mcp = FastMCP("k8s-generative")
mcp.tool_registry = {}  # Manual registry of all tools


# === Helper Decorator ===
def register_tool(func):
    """Registers tools in MCP and internal registry."""
    mcp.tool()(func)
    mcp.tool_registry[func.__name__] = func
    return func


# === Kubernetes Setup ===
def load_kube_config():
    try:
        config.load_kube_config()
    except Exception:
        try:
            config.load_incluster_config()
        except Exception as e:
            raise RuntimeError(f"Could not load Kubernetes configuration: {e}")


def get_clients():
    load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.AutoscalingV1Api()


def get_yaml_dir() -> Path:
    mcp_dir = os.getenv("MCP_DIR", ".")
    yaml_dir = Path(mcp_dir) / "yaml"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    return yaml_dir


# === TOOLS ===

@register_tool
def apply_yaml(yaml_content: str = None, yaml_path: str = None, filename: str = None) -> str:
    if not yaml_content and not yaml_path:
        raise ValueError("Either 'yaml_content' or 'yaml_path' must be provided.")

    load_kube_config()
    k8s_client = client.ApiClient()

    if yaml_content:
        yaml_dir = get_yaml_dir()
        filename = filename or "generated.yaml"
        yaml_path = yaml_dir / filename
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        print(f"[MCP] Saved YAML to {yaml_path}", file=sys.stderr)

    try:
        utils.create_from_yaml(k8s_client, str(yaml_path))
        print(f"[MCP] Applied YAML from {yaml_path}", file=sys.stderr)
        return f"Successfully applied manifests from {yaml_path}"
    except Exception as e:
        print(f"[MCP] Error applying YAML: {e}", file=sys.stderr)
        return f"Failed to apply {yaml_path}: {e}"


@register_tool
def delete_namespace(namespace: str) -> str:
    v1, _, _ = get_clients()
    try:
        v1.delete_namespace(name=namespace)
        return f"Namespace '{namespace}' deleted successfully."
    except ApiException as e:
        if e.status == 404:
            return f"Namespace '{namespace}' not found."
        return f"Failed to delete namespace '{namespace}': {e}"


@register_tool
def delete_deployment(name: str, namespace: str = "default") -> str:
    _, apps_v1, _ = get_clients()
    try:
        apps_v1.delete_namespaced_deployment(name=name, namespace=namespace)
        return f"Deployment '{name}' deleted successfully from '{namespace}'."
    except ApiException as e:
        if e.status == 404:
            return f"Deployment '{name}' not found in '{namespace}'."
        return f"Failed to delete deployment '{name}': {e}"


@register_tool
def delete_pod(name: str, namespace: str = "default") -> str:
    v1, _, _ = get_clients()
    try:
        v1.delete_namespaced_pod(name=name, namespace=namespace)
        return f"Pod '{name}' deleted successfully from '{namespace}'."
    except ApiException as e:
        if e.status == 404:
            return f"Pod '{name}' not found in '{namespace}'."
        return f"Failed to delete pod '{name}': {e}"


@register_tool
def delete_service(name: str, namespace: str = "default") -> str:
    v1, _, _ = get_clients()
    try:
        v1.delete_namespaced_service(name=name, namespace=namespace)
        return f"Service '{name}' deleted successfully from '{namespace}'."
    except ApiException as e:
        if e.status == 404:
            return f"Service '{name}' not found in '{namespace}'."
        return f"Failed to delete service '{name}': {e}"


@register_tool
def list_nodes():
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



@register_tool
def list_pods(namespace: str = None):
    v1, _, _ = get_clients()
    ret = (
        v1.list_namespaced_pod(namespace=namespace)
        if namespace
        else v1.list_pod_for_all_namespaces(watch=False)
    )
    result = []
    for i in ret.items:
        result.append({
            "pod_ip": i.status.pod_ip,
            "namespace": i.metadata.namespace,
            "name": i.metadata.name,
            "status": i.status.phase,  # <--- Add this line
            "created_at": i.metadata.creation_timestamp.isoformat()
        })
    return result


@register_tool
def list_services(namespace: str = "default"):
    v1, _, _ = get_clients()
    services = v1.list_namespaced_service(namespace=namespace)
    result = []
    for svc in services.items:
        ports = [{"port": p.port, "protocol": p.protocol} for p in svc.spec.ports]
        external_ips = (
            svc.status.load_balancer.ingress[0].ip if svc.status.load_balancer and svc.status.load_balancer.ingress else "N/A"
        )
        result.append({
            "name": svc.metadata.name,
            "type": svc.spec.type,
            "cluster_ip": svc.spec.cluster_ip,
            "external_ip": external_ips,
            "ports": ports
        })
    return result


@register_tool
def list_service_endpoints(namespace: str = "default"):
    v1, _, _ = get_clients()
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


@register_tool
def list_pods_with_logs(namespace: str = "default"):
    v1, _, _ = get_clients()
    pods = v1.list_namespaced_pod(namespace=namespace, watch=False)
    result = []
    for pod in pods.items:
        pod_info = {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "ip": pod.status.pod_ip,
            "status": pod.status.phase,
            "node": pod.spec.node_name,
            "containers": []
        }
        for container in pod.spec.containers:
            try:
                logs = v1.read_namespaced_pod_log(
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                    container=container.name,
                    tail_lines=5
                )
                pod_info["containers"].append({
                    "name": container.name,
                    "logs": logs
                })
            except Exception as e:
                pod_info["containers"].append({
                    "name": container.name,
                    "error": str(e)
                })
        result.append(pod_info)
    return result


@register_tool
def restart_deployment(deployment_name: str, namespace: str = "default"):
    _, apps_v1, _ = get_clients()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }
    apps_v1.patch_namespaced_deployment(name=deployment_name, namespace=namespace, body=body)
    return {"status": "success", "message": f"Deployment {deployment_name} restarted successfully in {namespace}."}


@register_tool
def scale_deployment(deployment_name: str, replicas: int, namespace: str = "default"):
    _, apps_v1, _ = get_clients()
    body = {"spec": {"replicas": int(replicas)}}
    apps_v1.patch_namespaced_deployment_scale(name=deployment_name, namespace=namespace, body=body)
    return {"status": "success", "message": f"Deployment {deployment_name} scaled to {replicas} in {namespace}."}


@register_tool
def list_deployments(namespace: str = "default"):
    _, apps_v1, _ = get_clients()
    deployments = apps_v1.list_namespaced_deployment(namespace=namespace)
    result = []
    for dep in deployments.items:
        images = [c.image for c in dep.spec.template.spec.containers]
        result.append({
            "name": dep.metadata.name,
            "replicas": dep.status.replicas or 0,
            "available": dep.status.available_replicas or 0,
            "images": images,
            "namespace": dep.metadata.namespace
        })
    return result


@register_tool
def create_namespace(name: str):
    v1, _, _ = get_clients()
    body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name))
    try:
        v1.create_namespace(body=body)
        return {"status": "success", "message": f"Namespace '{name}' created successfully."}
    except ApiException as e:
        if e.status == 409:
            return {"status": "exists", "message": f"Namespace '{name}' already exists."}
        return {"status": "error", "message": str(e)}


@register_tool
def create_deployment(namespace: str, name: str, image: str, replicas: int = 1, port: int = 80):
    config.load_kube_config()
    apps_v1 = client.AppsV1Api()

    # Define deployment manifest
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
        return {"status": "success", "message": f"Deployment '{name}' created in '{namespace}'."}
    except ApiException as e:
        if e.status == 409:
            return {"status": "exists", "message": f"Deployment '{name}' already exists in '{namespace}'."}
        else:
            return {"status": "error", "message": str(e)}


@register_tool
def list_namespaces():
    v1, _, _ = get_clients()
    namespaces = v1.list_namespace()
    result = []
    for ns in namespaces.items:
        result.append({
            "name": ns.metadata.name,
            "status": ns.status.phase,
            "created_at": ns.metadata.creation_timestamp.isoformat() if ns.metadata.creation_timestamp else "N/A"
        })
    return result


@register_tool
def create_service(namespace: str, name: str, deployment_name: str, port: int, target_port: int = None,
                   type: str = "ClusterIP", node_port: int = None):
    v1, _, _ = get_clients()
    port = int(port)
    target_port = int(target_port or port)
    service_port = client.V1ServicePort(port=port, target_port=target_port)
    if type == "NodePort" and node_port:
        service_port.node_port = int(node_port)
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
    v1.create_namespaced_service(namespace=namespace, body=body)
    msg = f"Service '{name}' created in '{namespace}' as type '{type}'."
    if type == "NodePort" and node_port:
        msg += f" NodePort: {node_port}"
    return {"status": "success", "message": msg}


@register_tool
def create_autoscaler(namespace: str, deployment_name: str, min_replicas: int, max_replicas: int, cpu_percent: int):
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
    autoscaling_v1.create_namespaced_horizontal_pod_autoscaler(namespace=namespace, body=body)
    return {"status": "success", "message": f"Autoscaler for '{deployment_name}' created successfully."}


# === HTTP SERVER ===

class MCPRequestHandler(BaseHTTPRequestHandler):
    def _send_response(self, code=200, data=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if data is not None:
            # ensure bytes
            self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/tools":
            tools_info = {}
            for name, func in mcp.tool_registry.items():
                tools_info[name] = {
                    "signature": {k: v.__name__ for k, v in func.__annotations__.items()},
                    "doc": func.__doc__ or ""
                }
            self._send_response(200, {"tools": tools_info})
        else:
            self._send_response(404, {"error": "Not found"})

    def do_POST(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path != "/run":
            self._send_response(404, {"error": "Not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        # log incoming request for debugging
        try:
            payload = json.loads(body)
        except Exception as e:
            # can't parse body
            self._send_response(400, {"error": f"Invalid JSON payload: {e}"})
            return

        # helpful debug print (goes to server console / journald)
        print(f"[MCP] Incoming request: {json.dumps(payload, ensure_ascii=False)}", file=sys.stderr)

        tool_name = payload.get("tool")
        args = payload.get("args", {}) or {}

        # validate presence
        if not tool_name:
            self._send_response(400, {"error": "Missing 'tool' in payload"})
            return

        # find tool
        if tool_name not in mcp.tool_registry:
            self._send_response(400, {"error": f"Tool '{tool_name}' not found"})
            return

        # run tool with guarded error capture
        try:
            result = mcp.tool_registry[tool_name](**args)
            # ensure result is JSON serializable; convert non-serializable to str
            self._send_response(200, result)
        except Exception as e:
            # print full traceback to stderr for debugging
            import traceback
            tb = traceback.format_exc()
            print("[MCP] ERROR while executing tool:", file=sys.stderr)
            print(tb, file=sys.stderr)
            # return safe error to caller; include first 1000 chars of traceback for debugging if desired
            # NOTE: remove trace in production to avoid leaking internals.
            self._send_response(500, {"error": str(e), "trace": tb[:1000]})



if __name__ == "__main__":
    server_address = ("0.0.0.0", 8000)
    httpd = HTTPServer(server_address, MCPRequestHandler)
    print(f"🚀 MCP HTTP server running at http://{server_address[0]}:{server_address[1]}/run ...")
    httpd.serve_forever()
