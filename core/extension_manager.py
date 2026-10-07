# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Catalog-backed install and update operations for third-party extensions."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core.extensions import user_plugins_dir
from core.version import USER_AGENT, __version__

CATALOG_URL = (
    "https://raw.githubusercontent.com/ingelibre/"
    "ingetrazo-extensions/main/catalog.json")
REVIEWED_URL = (
    "https://raw.githubusercontent.com/ingelibre/"
    "ingetrazo-extensions/main/reviewed.toml")
TIMEOUT = 20
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 2000
MAX_EXTRACTED_BYTES = 128 * 1024 * 1024
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ExtensionManagerError(Exception):
    """A catalog or installation operation could not be completed safely."""


@dataclass(frozen=True)
class Extension:
    id: str
    version: str
    author: str
    license: str
    repository: str
    download: str
    sha256: str
    minimum_version: str
    name: dict[str, str]
    summary: dict[str, str]
    tags: tuple[str, ...]
    screenshot: str | None = None


def _get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if not response.geturl().startswith("https://"):
                raise ExtensionManagerError(
                    "The download redirected to a non-HTTPS address.")
            data = response.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ExtensionManagerError(f"Could not download {url}: {exc}") from exc
    if len(data) > limit:
        raise ExtensionManagerError("The downloaded file is too large.")
    return data


def _text_map(value, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in value.items()):
        raise ExtensionManagerError(f"Invalid {field} in extension catalog.")
    return value


def _parse_extension(raw: object) -> Extension:
    if not isinstance(raw, dict):
        raise ExtensionManagerError("Invalid extension entry in catalog.")
    ident = raw.get("id")
    if not isinstance(ident, str) or not _ID_RE.fullmatch(ident):
        raise ExtensionManagerError("Invalid extension ID in catalog.")
    strings = ("version", "author", "license", "repository", "download",
               "sha256", "ingetrazo")
    if any(not isinstance(raw.get(key), str) or not raw[key]
           for key in strings):
        raise ExtensionManagerError(
            f"Extension {ident!r} has incomplete catalog metadata.")
    if not _SHA_RE.fullmatch(raw["sha256"]):
        raise ExtensionManagerError(
            f"Extension {ident!r} has an invalid SHA-256 digest.")
    if not raw["download"].startswith("https://"):
        raise ExtensionManagerError(
            f"Extension {ident!r} does not use an HTTPS download URL.")
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ExtensionManagerError(f"Extension {ident!r} has invalid tags.")
    screenshot = raw.get("screenshot")
    if screenshot is not None and (
            not isinstance(screenshot, str)
            or "/" in screenshot or "\\" in screenshot
            or PurePosixPath(screenshot).suffix.lower() not in (
                ".png", ".jpg", ".jpeg", ".webp")):
        raise ExtensionManagerError(
            f"Extension {ident!r} has an invalid screenshot filename.")
    return Extension(
        id=ident,
        version=raw["version"],
        author=raw["author"],
        license=raw["license"],
        repository=raw["repository"],
        download=raw["download"],
        sha256=raw["sha256"].lower(),
        minimum_version=raw["ingetrazo"],
        name=_text_map(raw.get("name"), "name"),
        summary=_text_map(raw.get("summary"), "summary"),
        tags=tuple(tags),
        screenshot=screenshot,
    )


def load_catalog() -> tuple[list[Extension], dict[str, dict]]:
    """Fetch and validate the catalog and maintainer-reviewed hashes."""
    try:
        catalog = json.loads(_get(CATALOG_URL, MAX_CATALOG_BYTES))
        import tomllib
        reviewed = tomllib.loads(_get(REVIEWED_URL, MAX_CATALOG_BYTES).decode(
            "utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExtensionManagerError(
            f"The extension catalog is invalid: {exc}") from exc
    if not isinstance(catalog, dict) or catalog.get("format") != 1:
        raise ExtensionManagerError("Unsupported extension catalog format.")
    raw_extensions = catalog.get("extensions")
    if not isinstance(raw_extensions, list):
        raise ExtensionManagerError("The extension catalog has no entries.")
    extensions = [_parse_extension(raw) for raw in raw_extensions]
    ids = [extension.id for extension in extensions]
    if len(ids) != len(set(ids)):
        raise ExtensionManagerError("The catalog contains duplicate IDs.")
    if not isinstance(reviewed, dict):
        raise ExtensionManagerError("The reviewed-extension list is invalid.")
    return extensions, reviewed


def display_name(extension: Extension, language: str) -> str:
    lang = language.lower().replace("_", "-").split("-", 1)[0]
    return (extension.name.get(lang) or extension.name.get("en")
            or next(iter(extension.name.values()), extension.id))


def display_summary(extension: Extension, language: str) -> str:
    lang = language.lower().replace("_", "-").split("-", 1)[0]
    return (extension.summary.get(lang) or extension.summary.get("en")
            or next(iter(extension.summary.values()), ""))


def is_reviewed(extension: Extension, reviewed: dict[str, dict]) -> bool:
    entry = reviewed.get(extension.id)
    return (isinstance(entry, dict)
            and str(entry.get("sha256", "")).lower() == extension.sha256)


def _managed_target(ident: str) -> tuple[Path, bool]:
    root = user_plugins_dir()
    folder = root / ident
    flat = root / f"{ident}.py"
    if folder.exists():
        if folder.is_symlink():
            return folder, False
        metadata = folder / "extension.json"
        if not folder.is_dir() or not metadata.is_file():
            return folder, False
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return folder, False
        return folder, isinstance(data, dict) and data.get("id") == ident
    if flat.exists():
        if flat.is_symlink():
            return flat, False
        return flat, False
    return folder, True


def installed_info(ident: str) -> dict | None:
    target, managed = _managed_target(ident)
    if not target.exists() or not managed or target.is_file():
        return None
    try:
        data = json.loads((target / "extension.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def installation_conflict(ident: str) -> bool:
    target, managed = _managed_target(ident)
    return target.exists() and not managed


def install_extension(extension: Extension) -> None:
    """Download, verify and atomically install an extension or its update."""
    target, managed = _managed_target(extension.id)
    if not managed:
        raise ExtensionManagerError(
            f"A plugin named {extension.id!r} already exists and is not "
            "managed by the extension manager.")
    archive_data = _get(extension.download, MAX_DOWNLOAD_BYTES)
    digest = hashlib.sha256(archive_data).hexdigest()
    if digest != extension.sha256:
        raise ExtensionManagerError(
            f"Checksum mismatch for {extension.id!r}; nothing was installed.")

    root = user_plugins_dir()
    root.mkdir(parents=True, exist_ok=True)
    keep_disabled = (target.is_dir()
                     and (target / ".disabled").is_file())
    stage = Path(tempfile.mkdtemp(prefix=f".{extension.id}-", dir=root))
    backup = root / f".{extension.id}-backup-{uuid.uuid4().hex}"
    try:
        is_zip = zipfile.is_zipfile(io.BytesIO(archive_data))
        if (urllib.parse.urlsplit(extension.download).path.lower().endswith(
                ".zip") and not is_zip):
            raise ExtensionManagerError(
                f"{extension.id!r} did not download a valid ZIP package.")
        if is_zip:
            _extract_plugin_zip(archive_data, stage, extension.id)
        else:
            if not archive_data or b"\x00" in archive_data:
                raise ExtensionManagerError(
                    f"{extension.id!r} is not a Python extension file.")
            (stage / "__init__.py").write_bytes(archive_data)
        metadata = {
            "id": extension.id,
            "version": extension.version,
            "sha256": extension.sha256,
            "download": extension.download,
        }
        (stage / "extension.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        if keep_disabled:
            (stage / ".disabled").write_text("", encoding="utf-8")
        if target.exists():
            target.replace(backup)
        try:
            stage.replace(target)
        except OSError:
            if backup.exists() and not target.exists():
                backup.replace(target)
            raise
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError:
                logging.getLogger("ingetrazo.extensions").warning(
                    "could not remove replaced extension backup %s",
                    backup, exc_info=True)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _extract_plugin_zip(data: bytes, stage: Path, ident: str) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExtensionManagerError("The extension ZIP file is invalid.") from exc
    with archive:
        members = [info for info in archive.infolist()
                   if not info.is_dir()
                   and not info.filename.startswith("__MACOSX/")]
        if len(members) > MAX_ARCHIVE_FILES:
            raise ExtensionManagerError("The extension archive has too many files.")
        total_size = 0
        normalized = []
        for info in members:
            name = info.filename
            path = PurePosixPath(name)
            mode = info.external_attr >> 16
            if ("\\" in name or path.is_absolute()
                    or any(part in ("", ".", "..") for part in path.parts)
                    or stat.S_ISLNK(mode)):
                raise ExtensionManagerError(
                    "The extension archive contains an unsafe path.")
            total_size += info.file_size
            if total_size > MAX_EXTRACTED_BYTES:
                raise ExtensionManagerError(
                    "The unpacked extension is too large.")
            normalized.append((info, path))

        init_files = [(info, path) for info, path in normalized
                      if path.name == "__init__.py"]
        if init_files:
            min_depth = min(len(path.parent.parts) for _info, path in init_files)
            roots = {path.parent for _info, path in init_files
                     if len(path.parent.parts) == min_depth}
            if len(roots) > 1:
                matching = [root for root in roots if root.name == ident]
                if len(matching) != 1:
                    raise ExtensionManagerError(
                        "The ZIP contains multiple top-level plugin packages.")
                source_root = matching[0]
            else:
                source_root = next(iter(roots))
            selected = [(info, path) for info, path in normalized
                        if path.is_relative_to(source_root)]
            _copy_archive_members(archive, selected, source_root, stage)
            return

        modules = [(info, path) for info, path in normalized
                   if path.suffix == ".py"]
        primary = [(info, path) for info, path in modules
                   if path.name == f"{ident}.py"]
        if len(primary) != 1:
            raise ExtensionManagerError(
                "The ZIP must contain one plugin package or a Python file "
                f"named {ident}.py.")
        info, path = primary[0]
        source_root = path.parent
        selected = [(member, member_path) for member, member_path in normalized
                    if member_path.is_relative_to(source_root)]
        for member, member_path in selected:
            relative = member_path.relative_to(source_root)
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
        (stage / path.name).replace(stage / "__init__.py")


def _copy_archive_members(archive, selected, source_root: PurePosixPath,
                          stage: Path) -> None:
    for info, path in selected:
        relative = path.relative_to(source_root)
        if not relative.parts:
            continue
        destination = stage.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive.read(info))


def set_extension_enabled(ident: str, enabled: bool) -> None:
    target, managed = _managed_target(ident)
    if not managed or not target.is_dir():
        raise ExtensionManagerError(
            f"{ident!r} is not installed by the extension manager.")
    marker = target / ".disabled"
    if enabled:
        marker.unlink(missing_ok=True)
    else:
        marker.write_text("", encoding="utf-8")


def is_extension_enabled(ident: str) -> bool:
    target, managed = _managed_target(ident)
    return managed and target.is_dir() and not (target / ".disabled").exists()


def compatible_with_current(extension: Extension) -> bool:
    def parts(value):
        match = re.match(r"^\s*(\d+(?:\.\d+)*)", value)
        return tuple(map(int, match.group(1).split("."))) if match else None
    required, current = parts(extension.minimum_version), parts(__version__)
    if required is None or current is None:
        return False
    width = max(len(required), len(current))
    return required + (0,) * (width - len(required)) <= \
        current + (0,) * (width - len(current))
