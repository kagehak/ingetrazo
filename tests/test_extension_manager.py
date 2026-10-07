# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from PySide6.QtWidgets import QApplication, QToolButton

from core import extension_manager as manager
from core.extensions import discover_plugins


TOOL_SOURCE = """
from tools.base import Tool

class CatalogTool(Tool):
    name = "Catalog tool"
    def on_activate(self, viewport):
        pass
    def on_deactivate(self, viewport):
        pass
"""


def _extension(payload: bytes, ident="sample") -> manager.Extension:
    return manager.Extension(
        id=ident,
        version="1.0",
        author="Author",
        license="MIT",
        repository="https://github.com/example/sample",
        download="https://example.org/sample.py",
        sha256=hashlib.sha256(payload).hexdigest(),
        minimum_version="0.1.0",
        name={"en": "Sample"},
        summary={"en": "Example extension"},
        tags=(),
    )


def test_catalog_validation_and_review_uses_the_reviewed_hash(monkeypatch):
    source = b"extension source"
    catalog = {
        "format": 1,
        "extensions": [{
            "id": "sample",
            "version": "1.0",
            "author": "Author",
            "license": "MIT",
            "repository": "https://github.com/example/sample",
            "download": "https://example.org/sample.py",
            "sha256": hashlib.sha256(source).hexdigest(),
            "ingetrazo": "0.5.0",
            "name": {"en": "Sample"},
            "summary": {"en": "Description"},
            "tags": [],
            "reviewed": True,
        }],
    }
    monkeypatch.setattr(
        manager, "_get",
        lambda url, _limit: (json.dumps(catalog).encode() if "catalog" in url
                             else b'[sample]\nsha256 = "'
                             + hashlib.sha256(source).hexdigest().encode()
                             + b'"\n'))
    extensions, reviewed = manager.load_catalog()
    assert manager.is_reviewed(extensions[0], reviewed)
    reviewed["sample"]["sha256"] = "0" * 64
    assert not manager.is_reviewed(extensions[0], reviewed)


def test_install_verify_disable_and_enable_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    extension = _extension(TOOL_SOURCE.encode())
    monkeypatch.setattr(manager, "_get", lambda *_args: TOOL_SOURCE.encode())
    manager.install_extension(extension)
    target = tmp_path / "sample"
    assert (target / "__init__.py").read_text() == TOOL_SOURCE
    assert manager.installed_info("sample")["version"] == "1.0"
    assert manager.is_extension_enabled("sample")
    assert [p.stem for p in discover_plugins([tmp_path])[0]] == ["sample"]

    manager.set_extension_enabled("sample", False)
    assert not manager.is_extension_enabled("sample")
    assert discover_plugins([tmp_path]) == ([], [])

    updated_source = TOOL_SOURCE + "\n# Updated package\n"
    monkeypatch.setattr(manager, "_get", lambda *_args: updated_source.encode())
    manager.install_extension(_extension(updated_source.encode()))
    assert not manager.is_extension_enabled("sample")
    assert discover_plugins([tmp_path]) == ([], [])

    manager.set_extension_enabled("sample", True)
    assert [p.stem for p in discover_plugins([tmp_path])[0]] == ["sample"]


def test_download_checksum_mismatch_never_creates_install(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    extension = _extension(b"expected")
    monkeypatch.setattr(manager, "_get", lambda *_args: b"tampered")
    with pytest.raises(manager.ExtensionManagerError, match="Checksum mismatch"):
        manager.install_extension(extension)
    assert not (tmp_path / "sample").exists()


def test_zip_package_is_extracted_and_traversal_is_rejected(
        tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("sample/__init__.py", TOOL_SOURCE)
        archive.writestr("sample/subpackage/__init__.py", "")
        archive.writestr("sample/helper.txt", "data")
    payload = output.getvalue()
    extension = _extension(payload)
    monkeypatch.setattr(manager, "_get", lambda *_args: payload)
    manager.install_extension(extension)
    assert (tmp_path / "sample" / "__init__.py").read_text() == TOOL_SOURCE
    assert (tmp_path / "sample" / "subpackage" / "__init__.py").is_file()
    assert (tmp_path / "sample" / "helper.txt").read_text() == "data"

    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../escape.py", "no")
        archive.writestr("sample.py", TOOL_SOURCE)
    malicious = bad.getvalue()
    monkeypatch.setattr(manager, "_get", lambda *_args: malicious)
    with pytest.raises(manager.ExtensionManagerError, match="unsafe path"):
        manager.install_extension(_extension(malicious, "other"))
    assert not (tmp_path.parent / "escape.py").exists()


def test_manual_plugin_collision_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    (tmp_path / "sample.py").write_text("user code")
    extension = _extension(b"catalog code")
    monkeypatch.setattr(manager, "_get", lambda *_args: b"catalog code")
    with pytest.raises(manager.ExtensionManagerError, match="not managed"):
        manager.install_extension(extension)
    assert (tmp_path / "sample.py").read_text() == "user code"


def test_minimum_version_comparison():
    extension = _extension(b"")
    assert manager.compatible_with_current(extension)
    assert manager.compatible_with_current(
        manager.Extension(**{**extension.__dict__,
                             "minimum_version": "99.0"})) is False


def test_extension_manager_search_tags_and_reviewed_filter(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    first = manager.Extension(
        **{**_extension(b"one", "loop_cut").__dict__,
           "tags": ("drawing", "productivity")})
    second = manager.Extension(
        **{**_extension(b"two", "terrain_tool").__dict__,
           "name": {"en": "Terrain Tool"},
           "summary": {"en": "Terrain editor"},
           "tags": ("terrain",)})
    reviewed = {"loop_cut": {"sha256": first.sha256}}
    from views import extension_manager_dialog as dialog_module
    monkeypatch.setattr(dialog_module, "load_catalog",
                        lambda: ([first, second], reviewed))
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    installed = tmp_path / "loop_cut"
    installed.mkdir()
    (installed / "extension.json").write_text(json.dumps({
        "id": "loop_cut",
        "version": "1.0",
        "sha256": first.sha256,
    }))
    dialog = dialog_module.ExtensionManagerDialog()
    try:
        assert len(dialog._filtered_extensions()) == 2
        dialog._pages["browse"]["search"].setText("editor")
        assert [ext.id for ext in dialog._filtered_extensions()] == [
            "terrain_tool"]

        dialog._pages["browse"]["search"].clear()
        terrain = next(button for button in
                       dialog._pages["browse"]["tags_group"].buttons()
                       if button.property("tag") == "terrain")
        terrain.click()
        assert [ext.id for ext in dialog._filtered_extensions()] == [
            "terrain_tool"]

        dialog._select_tag("browse", "")
        dialog._pages["browse"]["reviewed"].setChecked(True)
        assert [ext.id for ext in dialog._filtered_extensions()] == [
            "loop_cut"]

        assert [ext.id for ext in
                dialog._filtered_extensions("installed")] == ["loop_cut"]
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_extension_views_use_two_columns_and_16_by_10_previews(
        tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    extensions = [
        _extension(f"source {index}".encode(), f"extension_{index}")
        for index in range(5)
    ]
    from views import extension_manager_dialog as dialog_module
    monkeypatch.setattr(dialog_module, "load_catalog",
                        lambda: (extensions, {}))
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    dialog = dialog_module.ExtensionManagerDialog()
    try:
        dialog.resize(1180, 800)
        dialog.show()
        app.processEvents()
        grid = dialog._pages["browse"]["grid"]
        assert grid.count() == len(extensions)
        for index in range(grid.count()):
            _row, column, _row_span, _column_span = grid.getItemPosition(index)
            assert column == index % 2
        image = dialog._pages["browse"]["image_labels"]["extension_0"]
        assert image.width() * 10 == image.height() * 16
        assert image.width() > 200
        row = grid.itemAt(0).widget()
        assert row.width() > 400
        assert row.maximumWidth() <= dialog_module._ROW_MAX_WIDTH
        assert row.maximumHeight() == dialog_module._ROW_MAX_HEIGHT
        search = dialog._pages["browse"]["search"]
        assert search.parentWidget() is not None
        assert "background: #ffffff" in search.styleSheet()
        tags = dialog._pages["browse"]["tags_group"].buttons()
        assert tags and tags[0].text() == "All"
        assert dialog._pages["installed"]["grid"].count() == 0
        dialog._tabs.setCurrentIndex(1)
        assert dialog._pages["installed"]["grid"].count() == 0
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
