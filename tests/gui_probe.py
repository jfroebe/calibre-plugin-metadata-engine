"""Show a real Calibre-bundled Qt window to verify remote GUI input."""

import json
import os
from pathlib import Path

from qt.core import (
    QApplication,
    QLabel,
    QLineEdit,
    QPoint,
    QPushButton,
    QTimer,
    QVBoxLayout,
    QWidget,
)

app = QApplication([])
window = QWidget()
window.setWindowTitle("Metadata Calibre GUI control proof")
window.resize(520, 180)
layout = QVBoxLayout(window)
layout.addWidget(QLabel("Calibre Qt: type the test marker and click Record"))
entry = QLineEdit()
layout.addWidget(entry)
button = QPushButton("Record")
layout.addWidget(button)
status = QLabel("Waiting for automated input")
layout.addWidget(status)


def record() -> None:
    """Persist the typed marker to the disposable session report mount."""
    actual = entry.text()
    expected = os.environ["CALIBRE_GUI_PROOF_TOKEN"]
    result = {"expected": expected, "actual": actual, "passed": actual == expected}
    Path("/artifacts/input-proof.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    status.setText(
        "PASS: keyboard and button control"
        if result["passed"]
        else "FAIL: typed marker differs"
    )


button.clicked.connect(record)
window.show()
entry.setFocus()


def record_geometry() -> None:
    """Record the displayed button position for external mouse control."""
    center = button.mapToGlobal(QPoint(button.width() // 2, button.height() // 2))
    Path("/artifacts/target.json").write_text(
        json.dumps({"x": center.x(), "y": center.y()}), encoding="utf-8"
    )


QTimer.singleShot(1000, record_geometry)
QTimer.singleShot(90000, app.quit)
app.exec()
