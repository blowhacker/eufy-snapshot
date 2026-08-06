from __future__ import annotations

import json
import mimetypes
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ARTIFACT_MEDIA_TYPES = {
    ".ply": "application/octet-stream",
    ".json": "application/json",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class SpatialStoreError(ValueError):
    pass


class SpatialStore:
    """Read reconstruction manifests and their explicitly named artifacts.

    The directory shape is ``<scene>/<run>/manifest.json``. A scene owns an
    arbitrary camera set; a two-camera reconstruction is not special here.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def create_scene(
        self,
        name: str,
        camera_ids: list[str],
        feasibility: dict | str | None = None,
        *,
        feasibility_id: str | None = None,
    ) -> dict:
        """Persist a new scene with a queued reconstruction run.

        Scene and run identifiers are generated here rather than derived from
        the display name or camera identifiers.  ``feasibility`` may be a
        feasibility result object or (for callers with just a reference) its
        identifier; ``feasibility_id`` is the explicit spelling of the latter.
        """
        clean_name = self._validate_scene_name(name)
        clean_camera_ids = self._validate_camera_ids(camera_ids)
        feasibility_data = self._feasibility_data(feasibility, feasibility_id)

        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(32):
            scene_id = f"scene-{uuid.uuid4().hex}"
            run_id = f"run-{uuid.uuid4().hex}"
            run_dir = self.root / scene_id / run_id
            try:
                run_dir.mkdir(parents=True)
            except FileExistsError:
                continue

            created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            manifest = {
                "scene": {
                    "id": scene_id,
                    "name": clean_name,
                    "camera_ids": clean_camera_ids,
                },
                "run": {
                    "id": run_id,
                    "created_at": created_at,
                    "kind": "neural_reconstruction",
                    "metric": False,
                    "status": "queued",
                },
                "artifacts": {},
            }
            if feasibility_data is not None:
                manifest["feasibility"] = feasibility_data
            self._write_manifest(run_dir / "manifest.json", manifest)
            return manifest
        raise SpatialStoreError("could not allocate a unique scene run")

    def update_run(
        self,
        scene_id: str,
        run_id: str,
        *,
        status: str,
        artifacts: dict | None = None,
        stats: dict | None = None,
        warnings: list | None = None,
        error: str | None = None,
    ) -> dict:
        """Atomically update the persisted state of an existing run."""
        self._validate_identifier(scene_id, "scene")
        self._validate_identifier(run_id, "run")
        if status not in {"queued", "running", "ready", "failed"}:
            raise SpatialStoreError("invalid run status")
        if artifacts is not None and not isinstance(artifacts, dict):
            raise SpatialStoreError("artifacts must be an object")
        if stats is not None and not isinstance(stats, dict):
            raise SpatialStoreError("stats must be an object")
        if warnings is not None and not isinstance(warnings, list):
            raise SpatialStoreError("warnings must be a list")
        if error is not None and (not isinstance(error, str) or not error.strip()):
            raise SpatialStoreError("error must be a non-empty string")

        run_dir = self.root / scene_id / run_id
        manifest_path = run_dir / "manifest.json"
        manifest = self._read_manifest(manifest_path)
        if manifest["scene"]["id"] != scene_id or manifest["run"]["id"] != run_id:
            raise SpatialStoreError("manifest location does not match its identifiers")
        manifest["run"]["status"] = status
        manifest["run"]["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if artifacts is not None:
            manifest["artifacts"] = artifacts
        if stats is not None:
            manifest["stats"] = stats
        if warnings is not None:
            manifest["warnings"] = warnings
        if error is not None:
            manifest["error"] = error
        elif status != "failed":
            manifest.pop("error", None)
        self._read_manifest_data(manifest)
        self._write_manifest(manifest_path, manifest)
        return manifest

    def fail_run(self, scene_id: str, run_id: str, error: str) -> dict:
        return self.update_run(scene_id, run_id, status="failed", error=error)

    def complete_run(
        self, scene_id: str, run_id: str, *, artifacts: dict, stats: dict | None = None
    ) -> dict:
        return self.update_run(
            scene_id, run_id, status="ready", artifacts=artifacts, stats=stats
        )

    def list_scenes(self) -> list[dict]:
        scenes: dict[str, dict] = {}
        if not self.root.is_dir():
            return []

        for manifest_path in sorted(self.root.glob("*/*/manifest.json")):
            try:
                manifest = self._read_manifest(manifest_path)
            except (OSError, json.JSONDecodeError, SpatialStoreError):
                continue
            scene = manifest["scene"]
            run = manifest["run"]
            entry = scenes.setdefault(
                scene["id"],
                {
                    "id": scene["id"],
                    "name": scene["name"],
                    "camera_ids": scene["camera_ids"],
                    "runs": [],
                },
            )
            entry["runs"].append({
                **run,
                "artifacts": manifest["artifacts"],
                "stats": manifest.get("stats", {}),
                "warnings": manifest.get("warnings", []),
            })

        result = list(scenes.values())
        for scene in result:
            scene["runs"].sort(
                key=lambda run: (str(run.get("created_at", "")), run["id"]),
                reverse=True,
            )
        result.sort(key=lambda scene: scene["name"].casefold())
        return result

    def artifact(self, scene_id: str, run_id: str, artifact_name: str) -> tuple[Path, str]:
        for value, label in (
            (scene_id, "scene"), (run_id, "run"), (artifact_name, "artifact")
        ):
            if not _IDENTIFIER.fullmatch(value or ""):
                raise SpatialStoreError(f"invalid {label} identifier")

        run_dir = self.root / scene_id / run_id
        manifest = self._read_manifest(run_dir / "manifest.json")
        if manifest["scene"]["id"] != scene_id or manifest["run"]["id"] != run_id:
            raise SpatialStoreError("manifest location does not match its identifiers")

        relative = manifest["artifacts"].get(artifact_name)
        if not isinstance(relative, str) or not relative:
            raise SpatialStoreError("artifact is not present in this run")
        path = (run_dir / relative).resolve()
        try:
            path.relative_to(run_dir.resolve())
        except ValueError:
            raise SpatialStoreError("artifact escapes its run directory") from None
        if not path.is_file():
            raise FileNotFoundError(path)
        media_type = _ARTIFACT_MEDIA_TYPES.get(
            path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        return path, media_type

    def _read_manifest(self, path: Path) -> dict:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return self._read_manifest_data(manifest)

    def _read_manifest_data(self, manifest: object) -> dict:
        if not isinstance(manifest, dict):
            raise SpatialStoreError("manifest must be an object")
        scene = manifest.get("scene")
        run = manifest.get("run")
        artifacts = manifest.get("artifacts")
        if not isinstance(scene, dict) or not isinstance(run, dict) or not isinstance(artifacts, dict):
            raise SpatialStoreError("manifest is missing scene, run, or artifacts")

        scene_id = scene.get("id")
        run_id = run.get("id")
        camera_ids = scene.get("camera_ids")
        if not _IDENTIFIER.fullmatch(str(scene_id or "")):
            raise SpatialStoreError("invalid scene id")
        if not _IDENTIFIER.fullmatch(str(run_id or "")):
            raise SpatialStoreError("invalid run id")
        self._validate_scene_name(scene.get("name"))
        self._validate_camera_ids(camera_ids)
        if any(not _IDENTIFIER.fullmatch(str(name or "")) for name in artifacts):
            raise SpatialStoreError("invalid artifact name")
        return manifest

    @staticmethod
    def _validate_identifier(value: str, label: str) -> None:
        if not _IDENTIFIER.fullmatch(value or ""):
            raise SpatialStoreError(f"invalid {label} identifier")

    @staticmethod
    def _validate_scene_name(name: object) -> str:
        if not isinstance(name, str) or not (clean_name := name.strip()):
            raise SpatialStoreError("scene name is required")
        return clean_name

    @staticmethod
    def _validate_camera_ids(camera_ids: object) -> list[str]:
        if not isinstance(camera_ids, list) or not camera_ids:
            raise SpatialStoreError("scene camera_ids must be a non-empty list")
        if any(not isinstance(camera_id, str) or not _IDENTIFIER.fullmatch(camera_id) for camera_id in camera_ids):
            raise SpatialStoreError("invalid camera id")
        return list(camera_ids)

    @staticmethod
    def _feasibility_data(
        feasibility: dict | str | None, feasibility_id: str | None
    ) -> dict | None:
        if feasibility is not None and feasibility_id is not None:
            raise SpatialStoreError("provide feasibility or feasibility_id, not both")
        if isinstance(feasibility, str):
            feasibility_id = feasibility
            feasibility = None
        if feasibility_id is not None:
            if not isinstance(feasibility_id, str) or not feasibility_id.strip():
                raise SpatialStoreError("feasibility_id is required")
            return {"id": feasibility_id}
        if feasibility is not None:
            if not isinstance(feasibility, dict):
                raise SpatialStoreError("feasibility must be an object")
            try:
                json.dumps(feasibility)
            except (TypeError, ValueError) as exc:
                raise SpatialStoreError("feasibility must be JSON serializable") from exc
            return feasibility
        return None

    @staticmethod
    def _write_manifest(path: Path, manifest: dict) -> None:
        """Replace a manifest atomically so readers never see partial JSON."""
        try:
            encoded = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise SpatialStoreError("manifest must be JSON serializable") from exc
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".manifest-", delete=False) as temp:
            temp.write(encoded)
            temp.flush()
            os.fsync(temp.fileno())
            temp_path = Path(temp.name)
        try:
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
