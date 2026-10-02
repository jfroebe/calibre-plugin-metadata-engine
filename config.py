# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from qt.core import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox,
    QVBoxLayout, QWidget, Qt,
)

try:
    from calibre.gui2 import error_dialog
except Exception:
    error_dialog = None


class ConfigWidget(QWidget):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.setMinimumWidth(680)
        self._build_ui()
        self._load()
        self._sync_state()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(14)

        header = QFrame()
        header.setObjectName("metadataEngineHeader")
        h = QVBoxLayout(header)
        h.setContentsMargins(18, 14, 18, 14)
        h.addWidget(QLabel("<b style='font-size:16pt'>Metadata Engine</b>"))
        subtitle = QLabel("Use your metadata-engine server as a Calibre metadata and cover source.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: palette(mid);")
        h.addWidget(subtitle)
        outer.addWidget(header)

        connection = QGroupBox("Connection")
        form = QFormLayout(connection)

        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("http://127.0.0.1:8790")
        self.base_url.setClearButtonEnabled(True)
        form.addRow("Server URL:", self.base_url)

        self.api_token = QLineEdit()
        self.api_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_token.setPlaceholderText("Optional Bearer token")
        token_row = QWidget()
        token_layout = QHBoxLayout(token_row)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.addWidget(self.api_token, 1)
        self.show_token = QCheckBox("Show")
        self.show_token.toggled.connect(
            lambda show: self.api_token.setEchoMode(
                QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
            )
        )
        token_layout.addWidget(self.show_token)
        form.addRow("Authentication:", token_row)

        self.verify_ssl = QCheckBox("Verify HTTPS certificates")
        form.addRow("", self.verify_ssl)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test_connection)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row_layout.addWidget(self.test_button)
        row_layout.addWidget(self.status, 1)
        form.addRow("", row)
        outer.addWidget(connection)

        providers = QGroupBox("Metadata providers")
        pv = QVBoxLayout(providers)
        help_text = QLabel(
            "Automatic discovery uses every enabled metadata-engine provider. "
            "Choose selected providers to restrict Calibre."
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: palette(mid);")
        pv.addWidget(help_text)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Provider mode:"))
        self.provider_mode = QComboBox()
        self.provider_mode.addItem("Automatically use all enabled providers", "auto")
        self.provider_mode.addItem("Use only selected providers", "selected")
        self.provider_mode.currentIndexChanged.connect(self._sync_state)
        mode_row.addWidget(self.provider_mode, 1)
        pv.addLayout(mode_row)

        self.provider_list = QListWidget()
        self.provider_list.setAlternatingRowColors(True)
        self.provider_list.setMinimumHeight(150)
        pv.addWidget(self.provider_list)

        self.refresh_button = QPushButton("Refresh providers")
        self.refresh_button.clicked.connect(self._refresh)
        button_row = QHBoxLayout()
        button_row.addWidget(self.refresh_button)
        button_row.addStretch(1)
        pv.addLayout(button_row)
        outer.addWidget(providers)

        behavior = QGroupBox("Results and behavior")
        behavior_form = QFormLayout(behavior)

        self.max_results = QSpinBox()
        self.max_results.setRange(1, 100)
        self.max_results.setSuffix(" results")
        behavior_form.addRow("Maximum results:", self.max_results)

        self.per_plugin_limit = QSpinBox()
        self.per_plugin_limit.setRange(1, 100)
        self.per_plugin_limit.setSuffix(" per provider")
        behavior_form.addRow("Provider request limit:", self.per_plugin_limit)

        self.include_source_tag = QCheckBox("Add metadata-engine source name to Calibre tags")
        behavior_form.addRow("", self.include_source_tag)
        outer.addWidget(behavior)

        note = QLabel(
            "<b>Tip:</b> Automatic provider mode is recommended unless you need "
            "Calibre to query only a subset of metadata-engine providers."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid); padding: 4px;")
        outer.addWidget(note)
        outer.addStretch(1)

        self.setStyleSheet("""
            QGroupBox { font-weight: bold; margin-top: 10px; padding-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLineEdit, QComboBox, QSpinBox, QListWidget { padding: 5px; }
            QPushButton { padding: 5px 12px; }
            #metadataEngineHeader {
                border: 1px solid palette(midlight);
                border-radius: 8px;
                background: palette(alternate-base);
            }
        """)

    def _load(self):
        p = self.plugin.prefs
        self.base_url.setText(str(p.get("base_url", "http://127.0.0.1:8790") or ""))
        self.api_token.setText(str(p.get("api_token", "") or ""))
        self.verify_ssl.setChecked(bool(p.get("verify_ssl", True)))
        self.max_results.setValue(int(p.get("max_results", 20) or 20))
        self.per_plugin_limit.setValue(int(p.get("per_plugin_limit", 10) or 10))
        self.include_source_tag.setChecked(bool(p.get("include_source_tag", False)))

        selected = [x.strip() for x in str(p.get("plugin_ids", "") or "").split(",") if x.strip()]
        self._populate(selected, None)
        self.provider_mode.setCurrentIndex(
            self.provider_mode.findData("selected" if selected else "auto")
        )

    def commit(self):
        if not self.validate():
            return
        p = self.plugin.prefs
        p["base_url"] = self.base_url.text().strip().rstrip("/")
        p["api_token"] = self.api_token.text().strip()
        p["verify_ssl"] = self.verify_ssl.isChecked()
        p["max_results"] = self.max_results.value()
        p["per_plugin_limit"] = self.per_plugin_limit.value()
        p["include_source_tag"] = self.include_source_tag.isChecked()
        p["plugin_ids"] = ",".join(self._checked()) if self.provider_mode.currentData() == "selected" else ""

    def validate(self):
        url = self.base_url.text().strip()
        if not url:
            self._error("Metadata Engine URL is required.")
            return False
        if not (url.startswith("http://") or url.startswith("https://")):
            self._error("Metadata Engine URL must begin with http:// or https://.")
            return False
        if self.provider_mode.currentData() == "selected" and not self._checked():
            self._error("Select at least one provider or switch back to automatic mode.")
            return False
        return True

    def _test_connection(self):
        self.test_button.setEnabled(False)
        self.status.setText("Testing…")
        try:
            health = self._json("/health")
            plugins = self._json("/plugins").get("plugins") or []
            if health.get("ok"):
                self.status.setText("✓ Connected · %d provider%s" % (
                    len(plugins), "" if len(plugins) == 1 else "s"
                ))
                self._populate(self._checked(), plugins)
            else:
                self.status.setText("Connection returned an unhealthy status.")
        except Exception as exc:
            self.status.setText("✗ Connection failed")
            self._error("Could not connect to metadata-engine.", str(exc))
        finally:
            self.test_button.setEnabled(True)

    def _refresh(self):
        try:
            self._populate(self._checked(), self._json("/plugins").get("plugins") or [])
        except Exception as exc:
            self._error("Could not load metadata providers.", str(exc))

    def _json(self, path):
        base = self.base_url.text().strip().rstrip("/")
        if not base:
            raise RuntimeError("Server URL is empty")
        headers = {"Accept": "application/json", "User-Agent": "Calibre-Metadata-Engine-Config/1.1.0"}
        token = self.api_token.text().strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        context = None
        if base.startswith("https://") and not self.verify_ssl.isChecked():
            context = ssl._create_unverified_context()
        try:
            with urlopen(Request(base + path, headers=headers), timeout=8, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError("HTTP %s: %s" % (
                exc.code, exc.read().decode("utf-8", "replace") or exc.reason
            ))
        except URLError as exc:
            raise RuntimeError(str(exc.reason))

    def _sync_state(self):
        self.provider_list.setEnabled(self.provider_mode.currentData() == "selected")

    def _checked(self):
        out = []
        for row in range(self.provider_list.count()):
            item = self.provider_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.data(Qt.ItemDataRole.UserRole) or item.text())
        return out

    def _populate(self, selected, remote):
        selected = set(selected or [])
        self.provider_list.clear()
        entries = []
        if remote:
            for raw in remote:
                if isinstance(raw, str):
                    entries.append((raw, raw))
                elif isinstance(raw, dict) and raw.get("enabled", True):
                    pid = raw.get("plugin_id") or raw.get("id") or raw.get("name")
                    if pid:
                        label = raw.get("name") or pid
                        if raw.get("version"):
                            label = "%s · v%s" % (label, raw["version"])
                        entries.append((str(pid), str(label)))
        if not entries:
            entries = [(x, x) for x in sorted(selected)]

        for pid, label in sorted(entries, key=lambda x: x[1].casefold()):
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, pid)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if pid in selected else Qt.CheckState.Unchecked)
            self.provider_list.addItem(item)

        if not entries:
            placeholder = QListWidgetItem(
                "No providers loaded yet — use Test connection or Refresh providers."
            )
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.provider_list.addItem(placeholder)

    def _error(self, message, details=None):
        if error_dialog is not None:
            error_dialog(self, "Metadata Engine", message, det_msg=details or "", show=True)
        else:
            self.status.setText(message + ((": " + details) if details else ""))
