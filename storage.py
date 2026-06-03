"""
FROM SNOW TO RESERVOIR - Tile storage backend
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

The pipeline (download -> extract -> render) keeps the clipped OPERA GeoTIFFs in a
folder tree: OPERA_DSWx / {hls,s1} / {site} / *.tif. This module hides WHERE that
tree lives behind one small interface so the same scripts run two ways:

  drive (default) - Google Drive via pydrive2. Interactive OAuth on first use, then
                    a saved token. This is the persistent store for local runs (the
                    laptop is weak, Drive survives between sessions).
  local           - a directory under PIPELINE_LOCAL_DIR. No Google auth at all, so
                    it runs headless in CI: download + extract + render share one
                    temp dir within a single job, only NASA Earthdata is needed.

Choose the backend with the PIPELINE_STORE env var ("drive" or "local").

Both backends return tile handles as plain dicts with a "title" key, so callers can
keep using f["title"]; the bytes are always fetched via store.read_bytes(f), which
each backend implements for its own handle type.
"""

import io
import os
from pathlib import Path

# Sentinel parent id for the top-level OPERA folder. Equal to the literal "root"
# that Google Drive uses for the My Drive root, so old call sites passing "root"
# keep working; LocalStore maps it to the configured base directory.
ROOT = "root"


# ─────────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────────

class DriveStore:
    """OPERA tiles on Google Drive (pydrive2). Folder ids are Drive file ids."""

    def __init__(self):
        from pydrive2.auth import GoogleAuth
        from pydrive2.drive import GoogleDrive
        gauth = GoogleAuth()
        gauth.LoadCredentialsFile("gdrive_credentials.json")
        if gauth.credentials is None:
            gauth.LocalWebserverAuth()
        elif gauth.access_token_expired:
            gauth.Refresh()
        else:
            gauth.Authorize()
        gauth.SaveCredentialsFile("gdrive_credentials.json")
        self.drive = GoogleDrive(gauth)

    def get_folder_id(self, name: str, parent: str) -> str | None:
        query = (
            f"title='{name}' and mimeType='application/vnd.google-apps.folder' "
            f"and '{parent}' in parents and trashed=false"
        )
        results = self.drive.ListFile({"q": query}).GetList()
        return results[0]["id"] if results else None

    def ensure_folder(self, name: str, parent: str) -> str:
        fid = self.get_folder_id(name, parent)
        if fid:
            return fid
        folder = self.drive.CreateFile({
            "title": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [{"id": parent}],
        })
        folder.Upload()
        return folder["id"]

    def existing_names(self, folder: str) -> set[str]:
        """All filenames in a folder, paginated (Drive returns max 100 per page)."""
        names: set[str] = set()
        for page in self.drive.ListFile({
            "q": f"'{folder}' in parents and trashed=false",
            "maxResults": 1000,
        }):
            for f in page:
                names.add(f["title"])
        return names

    def list_tifs(self, folder: str) -> list:
        return self.drive.ListFile(
            {"q": f"'{folder}' in parents and trashed=false and title contains '.tif'"}
        ).GetList()

    def write(self, folder: str, name: str, data: bytes):
        f = self.drive.CreateFile({
            "title": name,
            "parents": [{"id": folder}],
            "mimeType": "image/tiff",
        })
        f.content = io.BytesIO(data)
        f.Upload()

    def read_bytes(self, f) -> bytes:
        return f.GetContentIOBuffer().read()


# ─────────────────────────────────────────────
# LOCAL FILESYSTEM
# ─────────────────────────────────────────────

class LocalStore:
    """OPERA tiles on disk, mirroring the Drive layout under a base directory.
    Folder 'ids' are absolute path strings; the ROOT sentinel maps to the base."""

    def __init__(self, base_dir: str | None = None):
        base = base_dir or os.environ.get("PIPELINE_LOCAL_DIR", "opera_local")
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)

    def _resolve(self, parent: str) -> Path:
        return self.base if parent == ROOT else Path(parent)

    def get_folder_id(self, name: str, parent: str) -> str | None:
        p = self._resolve(parent) / name
        return str(p) if p.is_dir() else None

    def ensure_folder(self, name: str, parent: str) -> str:
        p = self._resolve(parent) / name
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def existing_names(self, folder: str) -> set[str]:
        d = Path(folder)
        return {p.name for p in d.iterdir()} if d.is_dir() else set()

    def list_tifs(self, folder: str) -> list:
        d = Path(folder)
        if not d.is_dir():
            return []
        return [{"title": p.name, "_path": p} for p in sorted(d.glob("*.tif"))]

    def write(self, folder: str, name: str, data: bytes):
        (Path(folder) / name).write_bytes(data)

    def read_bytes(self, f) -> bytes:
        return f["_path"].read_bytes()


# ─────────────────────────────────────────────
# FACTORY
# ─────────────────────────────────────────────

def get_store():
    """Pick the backend from PIPELINE_STORE (default 'drive')."""
    backend = os.environ.get("PIPELINE_STORE", "drive").lower()
    if backend == "local":
        return LocalStore()
    return DriveStore()
