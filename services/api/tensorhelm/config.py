from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def split_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


class Settings(BaseSettings):
    """Hub API settings. Every field can be set with a TH_<NAME> environment variable."""

    model_config = SettingsConfigDict(env_prefix="TH_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./tensorhelm.db"

    # Authentication. "proxy": users sign in through the OpenShift oauth-proxy in front of the web pod,
    # which forwards X-Forwarded-User. "dev": every request is a fixed local user (never use in production).
    auth_mode: str = "dev"
    proxy_shared_secret: str = ""
    dev_user: str = "dev-admin"
    dev_groups: str = "tensorhelm-admins"
    admin_groups: str = "tensorhelm-admins"
    engineer_groups: str = "tensorhelm-engineers"
    group_cache_s: int = 60

    # Hub cluster and inventory.
    hub_cluster_name: str = "local-cluster"
    kube_enabled: bool = True
    acm_sync: bool = True
    refresh_interval_s: int = 30
    agent_offline_after_s: int = 180
    agent_bootstrap_token: str = ""
    agent_public_url: str = ""
    rhoai_applications_namespace: str = "redhat-ods-applications"

    # Delivery of deployments. "direct" applies to the hub cluster through the Kubernetes API;
    # "gitops" commits manifests to GitLab for Argo CD to apply on any managed cluster.
    delivery_mode: str = "direct"
    gitlab_url: str = ""
    gitlab_project: str = ""
    gitlab_token: str = ""
    gitlab_branch: str = "main"
    gitops_path_prefix: str = "deployments"

    # Model sources.
    huggingface_url: str = "https://huggingface.co"
    huggingface_token: str = ""
    model_registry_url: str = ""
    model_registry_token: str = ""
    openrouter_enabled: bool = False

    # Serving defaults.
    vllm_image: str = ""
    gpu_memory_utilization: float = 0.90
    outbound_timeout_s: float = 10.0

    @property
    def admin_group_set(self) -> set[str]:
        return split_csv(self.admin_groups)

    @property
    def engineer_group_set(self) -> set[str]:
        return split_csv(self.engineer_groups)

    @property
    def gitops_enabled(self) -> bool:
        return self.delivery_mode == "gitops" and bool(self.gitlab_url and self.gitlab_project)


@lru_cache
def get_settings() -> Settings:
    return Settings()
