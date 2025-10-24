from typing import Any, Dict, List
from common import (
    invalid_response,
    list_namespaces_cached,
    list_deployments_cached,
    list_services_cached,
    list_pods_cached,
)


class NamespaceValidator:
    def __init__(self, namespace: str):
        self.namespace = namespace

    def validate(self) -> Dict[str, Any] | None:
        if not self.namespace:
            return invalid_response("Missing namespace argument.")
        ns = list_namespaces_cached()
        if self.namespace not in ns:
            return invalid_response(f"Namespace '{self.namespace}' does not exist.", ns)
        return None


class DeploymentValidator(NamespaceValidator):
    def __init__(self, namespace: str, deployment_name: str):
        super().__init__(namespace)
        self.deployment_name = deployment_name

    def validate(self) -> Dict[str, Any] | None:
        base_err = super().validate()
        if base_err:
            return base_err
        if not self.deployment_name:
            return invalid_response("Missing deployment name argument.")
        deps = list_deployments_cached(self.namespace)
        if self.deployment_name not in deps:
            return invalid_response(
                f"Deployment '{self.deployment_name}' not found in namespace '{self.namespace}'.",
                deps,
            )
        return None


class ServiceValidator(NamespaceValidator):
    def __init__(self, namespace: str, service_name: str):
        super().__init__(namespace)
        self.service_name = service_name

    def validate(self) -> Dict[str, Any] | None:
        base_err = super().validate()
        if base_err:
            return base_err
        if not self.service_name:
            return invalid_response("Missing service name argument.")
        svcs = list_services_cached(self.namespace)
        if self.service_name not in svcs:
            return invalid_response(
                f"Service '{self.service_name}' not found in namespace '{self.namespace}'.",
                svcs,
            )
        return None


class PodValidator(NamespaceValidator):
    def __init__(self, namespace: str, pod_name: str):
        super().__init__(namespace)
        self.pod_name = pod_name

    def validate(self) -> Dict[str, Any] | None:
        base_err = super().validate()
        if base_err:
            return base_err
        if not self.pod_name:
            return invalid_response("Missing pod name argument.")
        pods = list_pods_cached(self.namespace)
        if self.pod_name not in pods:
            return invalid_response(
                f"Pod '{self.pod_name}' not found in namespace '{self.namespace}'.",
                pods,
            )
        return None
