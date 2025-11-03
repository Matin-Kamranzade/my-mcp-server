# k8s_utils.py
import os, time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from kubernetes import client, config

_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL = 30.0
kubeconfig_path = r"C:\Users\mkamranzada\.kube\config"
def load_kube_config():
    try:
        config.load_kube_config(config_file=kubeconfig_path)
    except Exception:
        try:
            config.load_incluster_config()
        except Exception as e:
            raise RuntimeError(f"Could not load Kubernetes configuration: {e}")

def get_clients():
    load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.AutoscalingV1Api()

def get_yaml_dir() -> Path:
    yaml_dir = Path(os.getenv("MCP_DIR", ".")) / "yaml"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    return yaml_dir

# --- caching ---
def _cache_get(key: str):
    entry = _CACHE.get(key)
    if not entry: return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return value

def _cache_set(key: str, value: Any):
    _CACHE[key] = (time.time(), value)

def _cache_invalidate(prefix: str):
    for k in list(_CACHE.keys()):
        if k.startswith(prefix):
            _CACHE.pop(k, None)
