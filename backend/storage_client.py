"""Simple local disk storage replacing Emergent object storage."""
import os
from typing import Tuple

APP_NAME = os.environ.get("APP_NAME", "docchat")
STORAGE_DIR = "/tmp/docchat_storage"


def init_storage() -> str:
    os.makedirs(STORAGE_DIR, exist_ok=True)
    return STORAGE_DIR


def put_object(path: str, data: bytes, content_type: str) -> dict:
    full_path = os.path.join(STORAGE_DIR, path.replace("/", "_"))
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(data)
    return {"path": path, "size": len(data)}


def get_object(path: str) -> Tuple[bytes, str]:
    full_path = os.path.join(STORAGE_DIR, path.replace("/", "_"))
    with open(full_path, "rb") as f:
        data = f.read()
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    mime_map = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    return data, mime_map.get(ext, "application/octet-stream")
