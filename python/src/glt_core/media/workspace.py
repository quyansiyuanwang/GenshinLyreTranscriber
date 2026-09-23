"""Validated per-job temporary workspace lifecycle."""

from __future__ import annotations

import pathlib
import re
import shutil
from types import TracebackType

from glt_core.media.ffmpeg import MediaError

JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class JobWorkspace:
    """A unique temporary directory whose cleanup cannot escape its root."""

    def __init__(self, root: pathlib.Path, job_id: str, path: pathlib.Path) -> None:
        self.root = root
        self.job_id = job_id
        self.path = path
        self._cleaned = False

    @classmethod
    def create(cls, root: pathlib.Path | str, job_id: str) -> JobWorkspace:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise MediaError("INVALID_JOB_ID", "job_id contains unsupported characters")
        resolved_root = pathlib.Path(root).expanduser().resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        workspace = resolved_root / job_id
        if workspace.exists():
            raise MediaError("WORKSPACE_EXISTS", f"job workspace already exists: {workspace}")
        workspace.mkdir()
        return cls(resolved_root, job_id, workspace.resolve())

    def cleanup(self) -> None:
        if self._cleaned:
            return
        resolved = self.path.resolve()
        if resolved.parent != self.root or not resolved.name:
            raise MediaError("UNSAFE_PATH", "refusing to clean a path outside the job root")
        if resolved.exists():
            shutil.rmtree(resolved)
        self._cleaned = True

    def __enter__(self) -> JobWorkspace:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()
