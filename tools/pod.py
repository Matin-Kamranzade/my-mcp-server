from typing import Any
from datetime import datetime, timezone
from kubernetes import client
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP
from common import _cache_invalidate, list_pods_cached, invalid_response
from validators import NamespaceValidator, PodValidator
from k8s_utils import get_clients, load_kube_config

MAX_LOG_BYTES = 10_000  # truncate logs after this many bytes
MAX_CONCURRENT = 5       # how many pods to fetch in parallel
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
        """Fetch pod logs safely and print them in readable form (flattened)."""
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            pods = v1.list_namespaced_pod(namespace=namespace, watch=False).items
            if not pods:
                return {"status": "ok", "message": f"No pods in namespace '{namespace}'."}

            results = []

            def fetch_pod_logs(pod):
                pod_summary = (
                    f"Pod: {pod.metadata.name} "
                    f"({pod.status.phase})\n"
                    f"Node: {pod.spec.node_name} | IP: {pod.status.pod_ip or 'N/A'}\n"
                )

                logs_texts = []
                for container in pod.spec.containers:
                    try:
                        logs = v1.read_namespaced_pod_log(
                            name=pod.metadata.name,
                            namespace=namespace,
                            container=container.name,
                            tail_lines=100,
                            timestamps=True,
                        ).strip()

                        if len(logs) > MAX_LOG_BYTES:
                            logs = logs[:MAX_LOG_BYTES] + "\n... (truncated)"

                        logs_texts.append(f"\n  Container: {container.name}\n" +
                                          "\n".join(f"    {line}" for line in logs.splitlines()))
                    except Exception as e:
                        logs_texts.append(f"\n  Container: {container.name}\n    (error fetching logs: {e})")

                full_text = pod_summary + "\n".join(logs_texts)
                return {"pod": pod.metadata.name, "namespace": namespace, "logs": full_text}

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
                results = list(executor.map(fetch_pod_logs, pods))

            # 🔹 Return one flattened list of readable log sections
            return "\n\n".join(r["logs"] for r in results)

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
    # ---------------- GET POD DETAILS ----------------
    @register(signature={'namespace': 'str', 'pod_name': 'str'})
    def get_pod_details(namespace: str, pod_name: str) -> Any:
        """Get comprehensive details about a specific Kubernetes pod."""
        pod_validator = PodValidator(namespace, pod_name)
        validation_error = pod_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)

            containers_info = []
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    containers_info.append({
                        "name": cs.name,
                        "image": cs.image,
                        "ready": cs.ready,
                        "restart_count": cs.restart_count,
                        "state": str(cs.state)
                    })

            return {
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "status": pod.status.phase,
                "node": pod.spec.node_name,
                "pod_ip": pod.status.pod_ip,
                "host_ip": pod.status.host_ip,
                "start_time": str(pod.status.start_time) if pod.status.start_time else None,
                "containers": containers_info,
                "conditions": [
                    {
                        "type": c.type,
                        "status": c.status,
                        "reason": c.reason,
                        "message": c.message
                    }
                    for c in (pod.status.conditions or [])
                ],
                "labels": pod.metadata.labels or {}
            }

        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return invalid_response(
                    f"Pod '{pod_name}' not found in '{namespace}'.",
                    list_pods_cached(namespace)
                )
            return {"status": "error", "message": str(e)}

    # ---------------- GET POD LOGS ----------------
    @register(signature={
        'namespace': 'str',
        'pod_name': 'str',
        'container': 'str|None',
        'tail_lines': 'int'
    })
    def get_pod_logs(namespace: str, pod_name: str, container: str = None, tail_lines: int = 100) -> Any:
        """Fetch recent logs from a specific pod within a namespace."""
        pod_validator = PodValidator(namespace, pod_name)
        validation_error = pod_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            kwargs = {
                "name": pod_name,
                "namespace": namespace,
                "tail_lines": tail_lines,
            }
            if container:
                kwargs["container"] = container

            logs = v1.read_namespaced_pod_log(**kwargs)
            if len(logs) > MAX_LOG_BYTES:
                logs = logs[:MAX_LOG_BYTES] + "\n... (truncated)"

            return {
                "namespace": namespace,
                "pod_name": pod_name,
                "container": container,
                "logs": logs
            }

        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return invalid_response(
                    f"Pod '{pod_name}' not found in '{namespace}'.",
                    list_pods_cached(namespace)
                )
            return {"status": "error", "message": str(e)}

    # ---------------- GET POD EVENTS ----------------
    @register(signature={'namespace': 'str', 'pod_name': 'str'})
    def get_pod_events(namespace: str, pod_name: str) -> Any:
        """Retrieve Kubernetes events associated with a specific pod."""
        pod_validator = PodValidator(namespace, pod_name)
        validation_error = pod_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            events = v1.list_namespaced_event(namespace=namespace)
            pod_events = [
                {
                    "type": e.type,
                    "reason": e.reason,
                    "message": e.message,
                    "count": e.count,
                    "first_timestamp": str(e.first_timestamp),
                    "last_timestamp": str(e.last_timestamp),
                }
                for e in events.items
                if e.involved_object.name == pod_name and e.involved_object.kind == "Pod"
            ]
            return sorted(pod_events, key=lambda x: x["last_timestamp"] or "", reverse=True)
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    # ---------------- GET PODS WITH ERRORS ----------------
    @register(signature={'namespace': 'str'})
    def get_pods_with_errors(namespace: str) -> Any:
        """List pods in a namespace that are not running or have error states."""
        ns_validator = NamespaceValidator(namespace)
        validation_error = ns_validator.validate()
        if validation_error:
            return validation_error

        v1, _, _ = get_clients()
        try:
            pods = v1.list_namespaced_pod(namespace)
            pods_with_errors = []

            for pod in pods.items:
                phase = pod.status.phase
                if phase not in ["Running", "Succeeded"]:
                    pods_with_errors.append({
                        "name": pod.metadata.name,
                        "phase": phase,
                        "reason": getattr(pod.status, "reason", None),
                        "message": getattr(pod.status, "message", None),
                    })
                    continue

                if pod.status.container_statuses:
                    for cs in pod.status.container_statuses:
                        st = cs.state
                        if st.waiting or (st.terminated and st.terminated.exit_code != 0):
                            pods_with_errors.append({
                                "name": pod.metadata.name,
                                "phase": phase,
                                "container": cs.name,
                                "state": "waiting" if st.waiting else "terminated",
                                "reason": getattr(st.waiting or st.terminated, "reason", None),
                                "exit_code": getattr(st.terminated, "exit_code", None) if st.terminated else None,
                            })

            return {"namespace": namespace, "pods_with_errors": pods_with_errors}
        except ApiException as e:
            return {"status": "error", "message": str(e)}

    return tools_dict
