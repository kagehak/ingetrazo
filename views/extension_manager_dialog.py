# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""Searchable community extension catalog and installer."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QStandardPaths, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import current_language, tr
from core.extension_manager import (
    Extension,
    ExtensionManagerError,
    compatible_with_current,
    display_name,
    display_summary,
    install_extension,
    installed_info,
    is_extension_enabled,
    is_reviewed,
    installation_conflict,
    load_catalog,
    set_extension_enabled,
)

log = logging.getLogger("ingetrazo.extensions")
_SCREENSHOT_BASE = (
    "https://raw.githubusercontent.com/ingelibre/"
    "ingetrazo-extensions/main/screenshots/")
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
_IMAGE_SIZE = (256, 160)  # 16:10
_ROW_MAX_HEIGHT = 300
_ROW_MAX_WIDTH = 660


class ExtensionManagerDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Manage extensions") + " — IngeTrazo")
        self.resize(1400, 860)
        self.setMinimumSize(1000, 650)
        self.setStyleSheet(
            "QDialog { background: #f5f6f8; }"
            "QScrollArea { background: #f5f6f8; border: none; }"
            "QScrollArea > QWidget > QWidget { background: #f5f6f8; }")
        self._extensions: list[Extension] = []
        self._reviewed: dict[str, dict] = {}
        self._requested_images: set[str] = set()
        self._network = QNetworkAccessManager(self)
        self._pages: dict[str, dict] = {}

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel(tr("Extensions"))
        heading.setStyleSheet("font-size: 24px; font-weight: bold")
        header.addWidget(heading)
        header.addStretch(1)
        self._refresh = QPushButton(tr("Refresh"))
        self._refresh.clicked.connect(self._load)
        header.addWidget(self._refresh)
        layout.addLayout(header)

        intro = QLabel(tr(
            "Tools the community adds to IngeTrazo. All are free software "
            "with published code; install only the ones you need."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_page("browse"), tr("Browse"))
        self._tabs.addTab(self._build_page("installed"), tr("Installed"))
        self._tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self._tabs, 1)

        footer = QHBoxLayout()
        warning = QLabel(tr(
            "Extensions are Python code. Only install extensions you trust."))
        warning.setWordWrap(True)
        footer.addWidget(warning, 1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)
        self._load()

    def _build_page(self, key: str) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 8, 0, 0)

        search = QLineEdit()
        search.setPlaceholderText(tr("Search extensions…"))
        search.setStyleSheet(
            "QLineEdit { background: #ffffff; color: #253247; "
            "border: 1px solid #d0d5dd; border-radius: 5px; "
            "padding: 7px 9px; }"
            "QLineEdit:focus { border-color: #3584e4; }")
        search_timer = QTimer(page)
        search_timer.setSingleShot(True)
        search_timer.setInterval(180)
        search_timer.timeout.connect(lambda page_key=key:
                                     self._render_page(page_key))
        search.textChanged.connect(lambda _text, timer=search_timer:
                                   timer.start())

        filters = QHBoxLayout()
        filters.addWidget(search, 1)
        reviewed = QCheckBox(tr("Reviewed only"))
        reviewed.setVisible(key == "browse")
        reviewed.toggled.connect(
            lambda _checked, page_key=key: self._render_page(page_key))
        filters.addWidget(reviewed)
        page_layout.addLayout(filters)

        tags_scroll = QScrollArea()
        tags_scroll.setFrameShape(QFrame.NoFrame)
        tags_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        tags_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tags_scroll.setFixedHeight(42)
        tags_scroll.setStyleSheet("QScrollArea { background: #f5f6f8; }")
        tags_content = QWidget()
        tags_content.setStyleSheet("background: #f5f6f8;")
        tags_layout = QHBoxLayout(tags_content)
        tags_layout.setContentsMargins(1, 2, 1, 2)
        tags_layout.setSpacing(6)
        tags_group = QButtonGroup(page)
        tags_group.setExclusive(True)
        tags_scroll.setWidget(tags_content)
        page.setStyleSheet("background: #f5f6f8;")
        page_layout.addWidget(tags_scroll)

        status = QLabel()
        status.setWordWrap(True)
        page_layout.addWidget(status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.verticalScrollBar().valueChanged.connect(
            self._load_visible_images)
        cards = QWidget()
        grid = QGridLayout(cards)
        grid.setContentsMargins(2, 2, 2, 12)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(18)
        grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 0)
        scroll.setWidget(cards)
        page_layout.addWidget(scroll, 1)

        self._pages[key] = {
            "search": search,
            "search_timer": search_timer,
            "tags_scroll": tags_scroll,
            "tags_layout": tags_layout,
            "tags_group": tags_group,
            "tag": "",
            "reviewed": reviewed,
            "status": status,
            "scroll": scroll,
            "cards": cards,
            "grid": grid,
            "image_labels": {},
        }
        return page

    def _load(self) -> None:
        self._refresh.setEnabled(False)
        active = self._active_page()
        active["status"].setText(tr("Loading extension catalog…"))
        try:
            self._extensions, self._reviewed = load_catalog()
            for key in self._pages:
                self._populate_tags(key)
                self._render_page(key)
        except ExtensionManagerError as exc:
            active["status"].setText(tr("Could not load extensions: {error}",
                                        error=str(exc)))
            for key in self._pages:
                self._clear_cards(key)
        finally:
            self._refresh.setEnabled(True)

    def _active_page_key(self) -> str:
        return "browse" if self._tabs.currentIndex() == 0 else "installed"

    def _active_page(self) -> dict:
        return self._pages[self._active_page_key()]

    def _populate_tags(self, key: str) -> None:
        page = self._pages[key]
        layout = page["tags_layout"]
        group = page["tags_group"]
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                group.removeButton(widget)
                widget.deleteLater()
        tags = sorted({tag for extension in self._extensions
                       for tag in extension.tags})
        self._add_tag_button(key, tr("All"), "")
        for tag in tags:
            self._add_tag_button(key, tr(tag.replace("-", " ").title()), tag)
        tags_content = page["tags_scroll"].widget()
        tags_content.adjustSize()
        tags_content.setFixedWidth(max(
            page["tags_scroll"].viewport().width(),
            tags_content.layout().sizeHint().width()))
        if page["tag"] and page["tag"] not in tags:
            page["tag"] = ""
        for button in group.buttons():
            if button.property("tag") == page["tag"]:
                button.setChecked(True)

    def _add_tag_button(self, key: str, label: str, tag: str) -> None:
        page = self._pages[key]
        button = QToolButton()
        button.setText(label)
        button.setCheckable(True)
        button.setProperty("tag", tag)
        button.setStyleSheet(
            "QToolButton { color: #344054; background: #ffffff; "
            "border: 1px solid #d0d5dd; border-radius: 12px; "
            "padding: 4px 10px; }"
            "QToolButton:checked { background: #3584e4; color: white; "
            "border-color: #3584e4; }")
        button.clicked.connect(
            lambda _checked=False, page_key=key, selected=tag:
            self._select_tag(page_key, selected))
        page["tags_group"].addButton(button)
        page["tags_layout"].addWidget(button)
        if tag == page["tag"]:
            button.setChecked(True)

    def _select_tag(self, key: str, tag: str) -> None:
        self._pages[key]["tag"] = tag
        self._render_page(key)

    def _clear_cards(self, key: str) -> None:
        page = self._pages[key]
        grid = page["grid"]
        while grid.count():
            item = grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        page["image_labels"].clear()

    def _filtered_extensions(self, key: str | None = None) -> list[Extension]:
        key = key or self._active_page_key()
        page = self._pages[key]
        query = page["search"].text().strip().casefold()
        installed_only = key == "installed"
        result = []
        for extension in self._extensions:
            info = installed_info(extension.id)
            if installed_only and info is None:
                continue
            if page["tag"] and page["tag"] not in extension.tags:
                continue
            if page["reviewed"].isChecked() and not is_reviewed(
                    extension, self._reviewed):
                continue
            searchable = " ".join((
                extension.id, extension.author, extension.license,
                extension.version, " ".join(extension.name.values()),
                " ".join(extension.summary.values()),
                " ".join(extension.tags),
            )).casefold()
            if query and query not in searchable:
                continue
            result.append(extension)
        return result

    def _render_page(self, key: str, *_args) -> None:
        page = self._pages[key]
        self._clear_cards(key)
        extensions = self._filtered_extensions(key)
        language = current_language()
        cell_width = max(
            400, min(_ROW_MAX_WIDTH,
                     (page["scroll"].viewport().width() - 40) // 2))
        for index, extension in enumerate(extensions):
            row = self._make_row(extension, language, page["cards"], key,
                                 cell_width)
            self._pages[key]["grid"].addWidget(row, index // 2, index % 2)
        page["status"].setText(tr("{shown} of {total} extensions",
                                  shown=len(extensions),
                                  total=(sum(installed_info(e.id) is not None
                                             for e in self._extensions)
                                         if key == "installed"
                                         else len(self._extensions))))
        if key == self._active_page_key():
            self._load_visible_images()

    def _on_tab_changed(self, _index: int) -> None:
        self._render_page(self._active_page_key())

    def _make_row(self, extension: Extension, language: str,
                  parent: QWidget, page_key: str, width: int) -> QWidget:
        info = installed_info(extension.id)
        managed = info is not None
        enabled = is_extension_enabled(extension.id) if managed else False
        current = managed and info.get("sha256") == extension.sha256
        compatible = compatible_with_current(extension)
        reviewed = is_reviewed(extension, self._reviewed)
        conflict = installation_conflict(extension.id)

        row = QWidget(parent)
        row.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        row.setFixedWidth(width)
        row.setMaximumHeight(_ROW_MAX_HEIGHT)
        row.setStyleSheet(
            "QWidget#extensionRow { background: #ffffff; "
            "border: 1px solid #d9dee5; }"
            "QWidget#extensionRow QLabel { background: transparent; "
            "border: none; }")
        row.setObjectName("extensionRow")
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(10, 10, 10, 8)
        row_layout.setSpacing(8)
        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(12)

        image = QLabel(tr("Preview unavailable"))
        image.setAlignment(Qt.AlignCenter)
        image.setFixedSize(*_IMAGE_SIZE)
        image.setStyleSheet(
            "background: #eef1f5; color: #667085; border: none;")
        self._pages[page_key]["image_labels"][extension.id] = image

        image_column = QVBoxLayout()
        image_column.setSpacing(6)
        image_column.addWidget(image, 0, Qt.AlignTop)
        byline = QLabel(tr("by {author}", author=extension.author))
        byline.setStyleSheet("color: #667085; font-size: 11px")
        image_column.addWidget(byline)
        tags_row = QHBoxLayout()
        tags_row.setSpacing(5)
        for tag in extension.tags:
            chip = QLabel(tr(tag.replace("-", " ").title()))
            chip.setStyleSheet(
                "color: #1769aa; background: #e8f1fc; "
                "border-radius: 8px; padding: 2px 6px; font-size: 10px")
            tags_row.addWidget(chip)
        tags_row.addStretch(1)
        image_column.addLayout(tags_row)
        version = QLabel(tr("Version {version}", version=extension.version))
        version.setStyleSheet(
            "font-weight: bold; color: #344054; font-size: 11px")
        image_column.addWidget(version)
        metadata = QLabel(
            f"{extension.license} · "
            + tr("Tested with IngeTrazo {version}",
                 version=extension.minimum_version))
        metadata.setWordWrap(True)
        metadata.setStyleSheet("color: #667085; font-size: 10px")
        image_column.addWidget(metadata)
        content.addLayout(image_column, 0)

        details = QVBoxLayout()
        details.setSpacing(5)
        title_row = QHBoxLayout()
        title = QLabel(display_name(extension, language))
        title.setWordWrap(True)
        title.setStyleSheet(
            "font-weight: bold; font-size: 15px; color: #253247; "
            "background: transparent;")
        title_row.addWidget(title, 1)
        if reviewed:
            badge = QLabel(tr("REVIEWED"))
            badge.setStyleSheet(
                "color: #287a32; border: 1px solid #8ac78f; "
                "border-radius: 3px; padding: 2px 5px; font-size: 9px; "
                "font-weight: bold")
            title_row.addWidget(badge, 0, Qt.AlignTop)
        details.addLayout(title_row)

        summary = QLabel(display_summary(extension, language))
        summary.setWordWrap(True)
        summary.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        summary.setMaximumHeight(100)
        details.addWidget(summary, 1)
        if managed:
            state = tr("Installed") if current else tr(
                "Installed version: {version}",
                version=info.get("version", tr("unknown")))
            if not enabled:
                state += " · " + tr("Disabled")
            details.addWidget(QLabel(state))
        elif conflict:
            details.addWidget(QLabel(
                tr("A plugin with this ID already exists.")))
        if not compatible:
            warning = QLabel(tr("Requires IngeTrazo {version} or later.",
                                version=extension.minimum_version))
            warning.setWordWrap(True)
            details.addWidget(warning)

        actions = QHBoxLayout()
        install = QPushButton(
            tr("Update") if managed and not current
            else tr("Installed") if managed
            else tr("Download"))
        install.setEnabled(compatible and not current and (managed or not conflict))
        install.clicked.connect(
            lambda _checked=False, e=extension: self._install(e))
        actions.addWidget(install)
        source = QPushButton(tr("Source code →"))
        source.clicked.connect(
            lambda _checked=False, url=extension.repository:
            QDesktopServices.openUrl(QUrl(url)))
        actions.addWidget(source)
        if managed:
            toggle = QPushButton(tr("Disable") if enabled else tr("Enable"))
            toggle.clicked.connect(
                lambda _checked=False, e=extension: self._toggle(e))
            actions.addWidget(toggle)
        actions.addStretch(1)
        details.addLayout(actions)
        content.addLayout(details, 1)
        row_layout.addLayout(content)

        return row

    def _screenshot_cache(self, extension: Extension) -> Path | None:
        if not extension.screenshot:
            return None
        base = Path(QStandardPaths.writableLocation(
            QStandardPaths.AppLocalDataLocation))
        return base / "extension-screenshots" / extension.screenshot

    def _load_visible_images(self, *_args) -> None:
        page = self._active_page()
        visible = page["scroll"].viewport().rect()
        for extension in self._filtered_extensions():
            label = page["image_labels"].get(extension.id)
            if label is None:
                continue
            label_rect = label.geometry()
            label_rect.moveTopLeft(label.mapTo(
                page["scroll"].viewport(), QPoint(0, 0)))
            if not visible.intersects(label_rect):
                continue
            cache = self._screenshot_cache(extension)
            if cache is None:
                continue
            if cache.is_file():
                try:
                    self._set_screenshot(extension.id, cache.read_bytes())
                except OSError:
                    log.warning("could not read extension screenshot cache %s",
                                cache, exc_info=True)
                continue
            if extension.id in self._requested_images:
                continue
            self._requested_images.add(extension.id)
            request = QNetworkRequest(QUrl(_SCREENSHOT_BASE
                                           + extension.screenshot))
            request.setAttribute(
                QNetworkRequest.RedirectPolicyAttribute,
                QNetworkRequest.NoLessSafeRedirectPolicy)
            reply = self._network.get(request)
            reply.finished.connect(
                lambda ident=extension.id, target=cache, r=reply:
                self._screenshot_finished(ident, target, r))

    def _screenshot_finished(self, ident: str, cache: Path, reply) -> None:
        self._requested_images.discard(ident)
        try:
            data = bytes(reply.readAll())
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            if (reply.error() != QNetworkReply.NetworkError.NoError
                    or status is None or not 200 <= int(status) < 300
                    or len(data) > _MAX_SCREENSHOT_BYTES):
                log.debug("could not fetch extension screenshot %s: %s",
                          ident, reply.errorString())
                return
            image = QPixmap()
            if not image.loadFromData(data):
                log.warning("invalid screenshot image for extension %s", ident)
                return
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(cache.suffix + ".part")
            temporary.write_bytes(data)
            temporary.replace(cache)
            self._set_screenshot(ident, data)
        except OSError:
            log.warning("could not cache extension screenshot %s",
                        ident, exc_info=True)
        finally:
            reply.deleteLater()

    def _set_screenshot(self, ident: str, data: bytes) -> None:
        label = self._active_page()["image_labels"].get(ident)
        if label is None:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            label.setPixmap(pixmap.scaled(
                label.size(), Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation))
            label.setText("")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._render_after_resize)

    def _render_after_resize(self) -> None:
        for key in self._pages:
            self._render_page(key)

    def _install(self, extension: Extension) -> None:
        try:
            install_extension(extension)
        except (ExtensionManagerError, OSError) as exc:
            QMessageBox.warning(self, tr("Extension manager"), str(exc))
            return
        QMessageBox.information(
            self, tr("Extension installed"),
            tr("«{name}» is installed. Restart IngeTrazo to load it.",
               name=display_name(extension, current_language())))
        for key in self._pages:
            self._render_page(key)

    def _toggle(self, extension: Extension) -> None:
        enabled = not is_extension_enabled(extension.id)
        try:
            set_extension_enabled(extension.id, enabled)
        except (ExtensionManagerError, OSError) as exc:
            QMessageBox.warning(self, tr("Extension manager"), str(exc))
            return
        action = tr("enabled") if enabled else tr("disabled")
        QMessageBox.information(
            self, tr("Extension changed"),
            tr("«{name}» is {state}. Restart IngeTrazo for the change to "
               "take effect.", name=display_name(extension, current_language()),
               state=action))
        for key in self._pages:
            self._render_page(key)
