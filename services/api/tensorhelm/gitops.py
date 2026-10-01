"""Commits rendered manifests to the GitOps repository in GitLab. Argo CD applies them to the target cluster."""

from urllib.parse import quote

import httpx


class GitOpsError(Exception):
    pass


class GitLabClient:
    def __init__(self, http: httpx.AsyncClient, url: str, project: str, token: str, branch: str):
        self.http = http
        self.api = f"{url.rstrip('/')}/api/v4/projects/{quote(project, safe='')}"
        self.headers = {"PRIVATE-TOKEN": token}
        self.branch = branch

    async def _exists(self, path: str) -> bool:
        response = await self.http.head(
            f"{self.api}/repository/files/{quote(path, safe='')}",
            params={"ref": self.branch},
            headers=self.headers,
        )
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise GitOpsError(f"GitLab returned {response.status_code} for {path}")
        return True

    async def commit(self, files: dict[str, str | None], message: str) -> str:
        """Create, update or (when the content is None) delete files in one commit. Returns the commit SHA."""
        actions = []
        for path, content in files.items():
            exists = await self._exists(path)
            if content is None:
                if exists:
                    actions.append({"action": "delete", "file_path": path})
                continue
            actions.append({"action": "update" if exists else "create", "file_path": path, "content": content})
        if not actions:
            raise GitOpsError("Nothing to commit")
        response = await self.http.post(
            f"{self.api}/repository/commits",
            json={"branch": self.branch, "commit_message": message, "actions": actions},
            headers=self.headers,
        )
        if response.status_code >= 400:
            raise GitOpsError(f"GitLab commit failed ({response.status_code}): {response.text[:300]}")
        return response.json()["id"]
