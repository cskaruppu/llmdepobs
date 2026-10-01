"""Tensorhelm spoke agent. Runs on each managed cluster, reads its GPU nodes, model endpoints and serving
runtimes, and reports them to the hub over an outbound HTTPS connection. It never accepts inbound traffic.

First start: registers with the hub using the bootstrap token, receives a cluster token and stores it in a
Secret so restarts reuse it. The cluster shows as "pending" in the portal until an admin approves it.
"""

import asyncio
import logging
import os
import signal

import httpx

from tensorhelm import __version__
from tensorhelm.inventory import collect

log = logging.getLogger("tensorhelm.agent")
TOKEN_SECRET = "tensorhelm-agent-credentials"


class AgentConfig:
    def __init__(self) -> None:
        self.hub_url = os.environ["TH_AGENT_HUB_URL"].rstrip("/")
        self.cluster_name = os.environ["TH_AGENT_CLUSTER_NAME"]
        self.bootstrap_token = os.environ.get("TH_AGENT_BOOTSTRAP_TOKEN", "")
        self.namespace = os.environ.get("TH_AGENT_NAMESPACE", "tensorhelm-agent")
        self.interval_s = int(os.environ.get("TH_AGENT_INTERVAL_S", "30"))
        self.rhoai_namespace = os.environ.get("TH_RHOAI_APPLICATIONS_NAMESPACE", "redhat-ods-applications")
        self.labels = dict(
            item.split("=", 1) for item in os.environ.get("TH_AGENT_LABELS", "").split(",") if "=" in item
        )
        ca_bundle = os.environ.get("TH_AGENT_CA_BUNDLE")
        insecure = os.environ.get("TH_AGENT_INSECURE_SKIP_VERIFY", "false").lower() == "true"
        self.verify: bool | str = False if insecure else (ca_bundle or True)


async def get_token(kube, cfg: AgentConfig, http: httpx.AsyncClient, facts: dict) -> str:
    token = await kube.read_secret_value(cfg.namespace, TOKEN_SECRET, "token")
    if token:
        return token
    if not cfg.bootstrap_token:
        raise RuntimeError("No stored cluster token and no TH_AGENT_BOOTSTRAP_TOKEN to register with")
    response = await http.post(
        f"{cfg.hub_url}/api/v1/agent/register",
        headers={"Authorization": f"Bearer {cfg.bootstrap_token}"},
        json={"cluster_name": cfg.cluster_name, "agent_version": __version__, "labels": cfg.labels, "facts": facts},
    )
    if response.status_code != 201:
        raise RuntimeError(f"Registration failed ({response.status_code}): {response.text[:300]}")
    token = response.json()["token"]
    await kube.write_secret(cfg.namespace, TOKEN_SECRET, {"token": token})
    log.info("registered %s with the hub; waiting for an admin to approve it", cfg.cluster_name)
    return token


async def report_once(kube, cfg: AgentConfig, http: httpx.AsyncClient, token: str | None) -> str:
    inventory = await collect(kube, cfg.rhoai_namespace)
    if token is None:
        token = await get_token(kube, cfg, http, inventory["facts"])
    response = await http.post(
        f"{cfg.hub_url}/api/v1/agent/report",
        headers={"Authorization": f"Bearer {token}"},
        json={"cluster_name": cfg.cluster_name, "agent_version": __version__, "labels": cfg.labels, **inventory},
    )
    if response.status_code == 401:
        raise PermissionError(
            "The hub rejected this cluster's token. Remove the cluster on the hub and the "
            f"{TOKEN_SECRET} Secret here, then restart the agent to register again."
        )
    response.raise_for_status()
    if not response.json().get("approved"):
        log.info("reported; still waiting for approval on the hub")
    return token


async def main() -> None:
    from tensorhelm.kube import KubeClient

    cfg = AgentConfig()
    kube = await KubeClient.connect()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    token: str | None = None
    async with httpx.AsyncClient(timeout=20, verify=cfg.verify) as http:
        while not stop.is_set():
            try:
                token = await report_once(kube, cfg, http, token)
            except PermissionError as exc:
                log.error("%s", exc)
            except Exception:
                log.exception("report failed; retrying")
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.interval_s)
            except TimeoutError:
                pass
    await kube.close()


def run() -> None:
    logging.basicConfig(
        level=os.environ.get("TH_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(main())


if __name__ == "__main__":
    run()
