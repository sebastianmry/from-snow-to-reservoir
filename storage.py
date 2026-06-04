"""
FROM SNOW TO RESERVOIR - Tile storage
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

The pipeline (download -> extract -> render) keeps the clipped OPERA GeoTIFFs in a
folder tree on disk: OPERA_DSWx / {hls,s1} / {site} / *.tif. This module hides the
mechanics of that tree behind one small interface so the scripts do not care about
filesystem details:

  ensure_folder(name, parent)  -> create/return a folder id
  get_folder_id(name, parent)  -> folder id or None
  existing_names(folder)       -> set of filenames already there
  list_tifs(folder)            -> tile handles in a folder
  write(folder, name, data)    -> store GeoTIFF bytes
  read_bytes(handle)           -> read a tile's bytes

The base directory is taken from the PIPELINE_LOCAL_DIR env var (default
"opera_local"). In CI it points at a temp dir so download + extract + render share
one store within a single job; locally it defaults to ./opera_local.

Tile handles are plain dicts with a "title" key, so callers can use f["title"];
the bytes are fetched via store.read_bytes(f).
"""

import os
from pathlib import Path

# Sentinel parent id for the top-level OPERA folder; LocalStore maps it to the
# configured base directory.
ROOT = "root"


class LocalStore:
    """OPERA tiles on disk under a base directory. Folder 'ids' are absolute path
    strings; the ROOT sentinel maps to the base directory."""

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


def get_store():
    """Open the tile store (local filesystem under PIPELINE_LOCAL_DIR)."""
    return LocalStore()
