#tool base
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from kubernetes import client, config, utils
from kubernetes.client.rest import ApiException

kubeconfig_path = r"C:\Users\mkamranzada\.kube\config"

# === CONFIGURATION ===
def load_kube_config():
    """Load kube config from local or in-cluster context."""
    try:
        config.load_kube_config(config_file=kubeconfig_path)
    except Exception:
        try:
            config.load_incluster_config()
        except Exception as e:
            raise RuntimeError(f"Could not load Kubernetes configuration: {e}")


def get_clients():
    """Return Kubernetes API clients."""
    load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.AutoscalingV1Api()


def get_yaml_dir() -> Path:
    """Return (and create if missing) the YAML directory."""
    mcp_dir = os.getenv("MCP_DIR", ".")
    yaml_dir = Path(mcp_dir) / "yaml"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    return yaml_dir


# === CACHE (30s TTL) ===
_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL = 30.0


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any):
    _CACHE[key] = (time.time(), value)


def _cache_invalidate(prefix: str):
    to_remove = [k for k in _CACHE.keys() if k.startswith(prefix)]
    for k in to_remove:
        _CACHE.pop(k, None)


# === CLUSTER LISTING HELPERS ===
def list_namespaces_cached() -> List[str]:
    cached = _cache_get("namespaces")
    if cached is not None:
        return cached
    v1, _, _ = get_clients()
    try:
        ns = v1.list_namespace()
        names = [n.metadata.name for n in ns.items]
        _cache_set("namespaces", names)
        return names
    except Exception:
        return []


def list_deployments_cached(namespace: str) -> List[str]:
    key = f"deployments::{namespace}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    _, apps_v1, _ = get_clients()
    try:
        deps = apps_v1.list_namespaced_deployment(namespace=namespace)
        names = [d.metadata.name for d in deps.items]
        _cache_set(key, names)
        return names
    except Exception:
        return []


def list_services_cached(namespace: str) -> List[str]:
    key = f"services::{namespace}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    v1, _, _ = get_clients()
    try:
        svcs = v1.list_namespaced_service(namespace=namespace)
        names = [s.metadata.name for s in svcs.items]
        _cache_set(key, names)
        return names
    except Exception:
        return []

def list_pods_cached(namespace: str) -> List[str]:
    key = f"pods::{namespace}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    v1, _, _ = get_clients()
    try:
        pods = v1.list_namespaced_pod(namespace=namespace)
        names = [p.metadata.name for p in pods.items]
        _cache_set(key, names)
        return names
    except Exception:
        return []
    
def invalid_response(msg: str, suggestion_list: List[str] = None) -> Dict[str, Any]:
    return {
        "error": msg,
        "suggestion": f"Try one of: {', '.join(suggestion_list)}" if suggestion_list else ""
    }
