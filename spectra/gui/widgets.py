from __future__ import annotations

import re

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPalette, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QApplication


class UiBridge(QObject):
    """Run callables on the GUI thread from any thread."""

    _sig = Signal(object)

    def __init__(self):
        super().__init__()
        self._sig.connect(self._run, Qt.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        fn()

    def post(self, fn):
        try:
            self._sig.emit(fn)
        except RuntimeError:
            pass  # the window was already closed

    def wrap(self, fn):
        """Return a function that, called from any thread, runs fn(*args) on the GUI thread."""
        return lambda *a: self.post(lambda: fn(*a))


def fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    return f


class SelectorHighlighter(QSyntaxHighlighter):
    RULES = [
        (re.compile(r"^\s*(?:>>?\s*)?(uia|web|java):"), fmt("#c586c0", bold=True)),
        (re.compile(r"(?<![\w\"'])[A-Za-z_#][\w.#\-]*(?=[\[:\s>]|$)"), fmt("#4fc1ff", bold=True)),
        (re.compile(r"\[\s*([\w\-]+)"), fmt("#9cdcfe")),
        (re.compile(r"!=|\*=|\^=|\$=|~=|\?=|="), fmt("#d4d4d4")),
        (re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"), fmt("#ce9178")),
        (re.compile(r":(nth|first|last|visible)(\(-?\d+\))?"), fmt("#dcdcaa")),
        (re.compile(r">>|>"), fmt("#ffcc66", bold=True)),
    ]

    def highlightBlock(self, text):
        for rx, f in self.RULES:
            for m in rx.finditer(text):
                if rx.groups and m.lastindex:
                    self.setFormat(m.start(m.lastindex), m.end(m.lastindex) - m.start(m.lastindex), f)
                else:
                    self.setFormat(m.start(), m.end() - m.start(), f)


def apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    base, alt, text, hl = QColor(30, 31, 34), QColor(40, 42, 46), QColor(220, 222, 226), QColor(20, 184, 166)
    p.setColor(QPalette.Window, QColor(36, 37, 41))
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, alt)
    p.setColor(QPalette.ToolTipBase, alt)
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, QColor(48, 50, 55))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.Highlight, hl)
    p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.PlaceholderText, QColor(130, 133, 140))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(110, 112, 118))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(110, 112, 118))
    app.setPalette(p)
    app.setStyleSheet("""
        QToolBar { spacing: 4px; padding: 3px; border: none; }
        QToolButton { padding: 4px 8px; border-radius: 4px; }
        QToolButton:hover { background: #3a3d44; }
        QToolButton:checked { background: #134e4a; }
        QDockWidget::title { padding: 4px 8px; background: #2b2d31; }
        QHeaderView::section { background: #2b2d31; padding: 4px; border: none; border-right: 1px solid #3a3d44; }
        QPlainTextEdit, QLineEdit, QTableWidget, QTreeWidget, QListWidget { border: 1px solid #3a3d44; border-radius: 4px; }
        QPushButton { padding: 5px 12px; border-radius: 4px; background: #3a3d44; border: 1px solid #4a4d55; }
        QPushButton:hover { background: #474b53; }
        QPushButton#primary { background: #0d9488; border-color: #14b8a6; color: white; }
        QPushButton#primary:hover { background: #14b8a6; }
        QLabel#status_ok { color: #6fdc8c; font-weight: bold; }
        QLabel#status_warn { color: #ffcc66; font-weight: bold; }
        QLabel#status_bad { color: #ff6b6b; font-weight: bold; }
        QTabBar::tab { padding: 6px 14px; }
        QTableView::indicator, QListView::indicator, QCheckBox::indicator {
            width: 13px; height: 13px; border: 1px solid #7a7d85; border-radius: 3px; background: #1e1f22; }
        QTableView::indicator:checked, QListView::indicator:checked, QCheckBox::indicator:checked {
            background: #14b8a6; border-color: #2dd4bf; }
    """)
