# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)

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


def _stub_catalog(monkeypatch, module, extensions, reviewed=None):
    snapshot = manager.CatalogSnapshot(
        extensions, reviewed or {}, "2026-10-07T10:00:00+00:00", False)
    monkeypatch.setattr(module, "load_cached_catalog", lambda: snapshot)
    monkeypatch.setattr(module, "refresh_catalog", lambda: snapshot)


def _wait_for_catalog(dialog, app):
    if dialog._catalog_worker is None:
        return
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(
        lambda: loop.quit() if dialog._catalog_worker is None else None)
    timer.start()
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    timer.stop()
    assert dialog._catalog_worker is None


def test_catalog_validation_and_review_uses_the_reviewed_hash(
        tmp_path, monkeypatch):
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
    monkeypatch.setattr(manager, "catalog_cache_path",
                        lambda: tmp_path / "catalog-cache.json")
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


def test_catalog_is_cached_and_used_when_refresh_is_offline(
        tmp_path, monkeypatch):
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
        }],
    }
    reviewed = b'[sample]\nsha256 = "' + hashlib.sha256(source).hexdigest().encode() + b'"\n'
    monkeypatch.setattr(manager, "catalog_cache_path",
                        lambda: tmp_path / "catalog-cache.json")
    monkeypatch.setattr(
        manager, "_get",
        lambda url, _limit: (json.dumps(catalog).encode()
                             if "catalog" in url else reviewed))
    live = manager.refresh_catalog()
    assert not live.from_cache
    assert manager.load_cached_catalog().extensions[0].id == "sample"

    def offline(*_args):
        raise manager.ExtensionManagerError("offline")
    monkeypatch.setattr(manager, "_get", offline)
    cached = manager.refresh_catalog()
    assert cached.from_cache
    assert cached.fetched_at == live.fetched_at
    assert cached.extensions[0].id == "sample"


def test_catalog_refresh_without_cache_surfaces_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "catalog_cache_path",
                        lambda: tmp_path / "missing-cache.json")
    monkeypatch.setattr(manager, "_get", lambda *_args: (_ for _ in ()).throw(
        manager.ExtensionManagerError("offline")))
    with pytest.raises(manager.ExtensionManagerError, match="offline"):
        manager.refresh_catalog()


def test_install_verify_disable_and_enable_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    extension = _extension(TOOL_SOURCE.encode())
    monkeypatch.setattr(manager, "_get", lambda *_args: TOOL_SOURCE.encode())
    manager.install_extension(extension)
    target = tmp_path / "sample"
    assert (target / "__init__.py").read_text() == TOOL_SOURCE
    assert manager.installed_info("sample")["version"] == "1.0"
    assert manager.is_extension_enabled("sample")
    assert manager.restart_required("sample", {})
    assert [p.stem for p in discover_plugins([tmp_path])[0]] == ["sample"]

    manager.set_extension_enabled("sample", False)
    assert not manager.is_extension_enabled("sample")
    assert not manager.restart_required("sample", {})
    assert discover_plugins([tmp_path]) == ([], [])

    updated_source = TOOL_SOURCE + "\n# Updated package\n"
    monkeypatch.setattr(manager, "_get", lambda *_args: updated_source.encode())
    manager.install_extension(_extension(updated_source.encode()))
    assert not manager.is_extension_enabled("sample")
    assert not manager.restart_required("sample", {})
    assert discover_plugins([tmp_path]) == ([], [])

    manager.set_extension_enabled("sample", True)
    assert manager.restart_required("sample", {})
    assert [p.stem for p in discover_plugins([tmp_path])[0]] == ["sample"]
    startup = manager.extension_startup_state()
    assert not manager.restart_required("sample", startup)
    monkeypatch.setattr(manager, "_get",
                        lambda *_args: (updated_source + "# newer\n").encode())
    newer = (updated_source + "# newer\n").encode()
    manager.install_extension(_extension(newer))
    assert manager.restart_required("sample", startup)


def test_restart_required_detects_disable_against_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    source = TOOL_SOURCE.encode()
    monkeypatch.setattr(manager, "_get", lambda *_args: source)
    manager.install_extension(_extension(source))
    startup = manager.extension_startup_state()
    assert not manager.restart_required("sample", startup)
    manager.set_extension_enabled("sample", False)
    assert manager.restart_required("sample", startup)


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


def test_tested_version_age_is_a_nonblocking_warning():
    extension = _extension(b"")
    assert manager.tested_version_is_old(
        manager.Extension(**{**extension.__dict__,
                             "minimum_version": "0.0.0"}))


def test_extension_sort_modes_and_recent_date_availability(
        tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    alpha = manager.Extension(
        **{**_extension(b"alpha", "alpha").__dict__,
           "name": {"en": "Alpha"},
           "updated_at": "2026-01-01"})
    beta = manager.Extension(
        **{**_extension(b"beta", "beta").__dict__,
           "name": {"en": "Beta"},
           "updated_at": "2026-03-01"})
    from views import extension_manager_dialog as dialog_module
    _stub_catalog(monkeypatch, dialog_module, [alpha, beta],
                  {"beta": {"sha256": beta.sha256}})
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    for extension, digest in ((alpha, "old-hash"), (beta, beta.sha256)):
        target = tmp_path / extension.id
        target.mkdir()
        (target / "extension.json").write_text(json.dumps({
            "id": extension.id,
            "version": "0.9" if extension.id == "alpha" else "1.0",
            "sha256": digest,
        }))
    dialog = dialog_module.ExtensionManagerDialog()
    try:
        _wait_for_catalog(dialog, app)
        page = dialog._pages["browse"]
        sort = page["sort"]
        sort.setCurrentIndex(sort.findData("name"))
        assert [ext.id for ext in dialog._filtered_extensions()] == [
            "alpha", "beta"]
        sort.setCurrentIndex(sort.findData("reviewed"))
        assert [ext.id for ext in dialog._filtered_extensions()] == [
            "beta", "alpha"]
        sort.setCurrentIndex(sort.findData("updates"))
        assert [ext.id for ext in dialog._filtered_extensions()] == [
            "alpha", "beta"]
        sort.setCurrentIndex(sort.findData("recent"))
        assert [ext.id for ext in dialog._filtered_extensions()] == [
            "beta", "alpha"]
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()

    no_date_entries = [
        manager.Extension(**{**extension.__dict__, "updated_at": None})
        for extension in (alpha, beta)
    ]
    _stub_catalog(monkeypatch, dialog_module, no_date_entries)
    no_dates = dialog_module.ExtensionManagerDialog()
    try:
        _wait_for_catalog(no_dates, app)
        recent = no_dates._pages["browse"]["sort"].model().item(
            no_dates._pages["browse"]["sort"].findData("recent"))
        assert not recent.isEnabled()
    finally:
        no_dates.close()
        no_dates.deleteLater()
        app.processEvents()


def test_failed_extension_card_offers_recovery_actions(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    extension = _extension(TOOL_SOURCE.encode())
    from views import extension_manager_dialog as dialog_module
    _stub_catalog(monkeypatch, dialog_module, [extension])
    monkeypatch.setattr(dialog_module, "extension_startup_state", lambda: {})
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    target = tmp_path / extension.id
    target.mkdir()
    (target / "extension.json").write_text(json.dumps({
        "id": extension.id,
        "version": extension.version,
        "sha256": extension.sha256,
    }))
    parent = QWidget()
    parent._extension_load_errors = {
        extension.id: {"error": "ImportError: missing dependency",
                       "path": str(target / "__init__.py")}
    }
    dialog = dialog_module.ExtensionManagerDialog(parent)
    try:
        _wait_for_catalog(dialog, app)
        labels = dialog._pages["browse"]["cards"].findChildren(QLabel)
        assert any(label.text() == "Failed to load at startup"
                   for label in labels)
        buttons = dialog._pages["browse"]["cards"].findChildren(QPushButton)
        names = {button.text() for button in buttons}
        assert {"View load error", "Disable extension",
                "Open extension folder", "Open logs folder"} <= names
    finally:
        dialog.close()
        dialog.deleteLater()
        parent.deleteLater()
        app.processEvents()


def test_unexpected_catalog_refresh_error_keeps_cached_catalog_visible(
        monkeypatch):
    app = QApplication.instance() or QApplication([])
    extension = _extension(TOOL_SOURCE.encode())
    from views import extension_manager_dialog as dialog_module
    snapshot = manager.CatalogSnapshot(
        [extension], {}, "2026-10-07T10:00:00+00:00", True)
    monkeypatch.setattr(dialog_module, "load_cached_catalog",
                        lambda: snapshot)

    def fail_refresh():
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(dialog_module, "refresh_catalog", fail_refresh)
    dialog = dialog_module.ExtensionManagerDialog()
    try:
        _wait_for_catalog(dialog, app)
        assert [item.id for item in dialog._extensions] == [extension.id]
        assert "Could not refresh catalog" in (
            dialog._pages["browse"]["status"].text())
        assert dialog._refresh.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


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
    _stub_catalog(monkeypatch, dialog_module, [first, second], reviewed)
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
        _wait_for_catalog(dialog, app)
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
    _stub_catalog(monkeypatch, dialog_module, extensions)
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    dialog = dialog_module.ExtensionManagerDialog()
    try:
        _wait_for_catalog(dialog, app)
        dialog.resize(1180, 800)
        dialog.show()
        app.processEvents()
        grid = dialog._pages["browse"]["grid"]
        assert grid.count() == len(extensions)
        for index in range(grid.count()):
            _row, column, _row_span, _column_span = grid.getItemPosition(index)
            assert column == index % 2
            assert grid.itemAt(index).widget().width() >= 400
        image = dialog._pages["browse"]["image_labels"]["extension_0"]
        assert image.width() * 10 == image.height() * 16
        assert image.width() > 200
        row = grid.itemAt(0).widget()
        assert row.width() >= int(
            dialog._pages["browse"]["scroll"].viewport().width() * 0.4)
        assert row.maximumWidth() <= dialog_module._ROW_MAX_WIDTH
        assert row.maximumHeight() == dialog_module._ROW_MAX_HEIGHT
        search = dialog._pages["browse"]["search"]
        assert search.parentWidget() is not None
        assert "background: #ffffff" in search.styleSheet()
        tags = dialog._pages["browse"]["tags_group"].buttons()
        assert tags and tags[0].text() == "All"
        assert dialog._pages["browse"]["tags_layout"].spacing() == 8
        assert dialog._pages["installed"]["tags_layout"].spacing() == 8
        assert all(button.sizePolicy().horizontalPolicy()
                   == dialog_module.QSizePolicy.Fixed for button in tags)
        assert dialog._pages["browse"]["filters"].contentsMargins().right() \
            == 10
        tags_scroll = dialog._pages["browse"]["tags_scroll"]
        assert tags_scroll.isVisible()
        assert all(button.isVisible() for button in tags)
        sort = dialog._pages["browse"]["sort"]
        assert "padding: 5px 8px" in sort.styleSheet()
        assert "QComboBox:hover { color: #253247" in sort.styleSheet()
        assert dialog._pages["installed"]["grid"].count() == 0
        dialog._tabs.setCurrentIndex(1)
        assert dialog._pages["installed"]["grid"].count() == 0
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_installed_card_shows_restart_status(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    extension = _extension(TOOL_SOURCE.encode())
    from views import extension_manager_dialog as dialog_module
    _stub_catalog(monkeypatch, dialog_module, [extension])
    monkeypatch.setattr(dialog_module, "extension_startup_state", lambda: {})
    monkeypatch.setattr(manager, "user_plugins_dir", lambda: tmp_path)
    target = tmp_path / extension.id
    target.mkdir()
    (target / "extension.json").write_text(json.dumps({
        "id": extension.id,
        "version": extension.version,
        "sha256": extension.sha256,
    }))
    dialog = dialog_module.ExtensionManagerDialog()
    try:
        _wait_for_catalog(dialog, app)
        labels = dialog._pages["browse"]["cards"].findChildren(
            QLabel)
        assert any(label.text() == "Restart required to apply changes"
                   for label in labels)
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
