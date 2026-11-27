"""Spectra Inspector main window."""
from __future__ import annotations

import json
import os
import socket
import time
from typing import List, Optional

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)
from shiboken6 import isValid

from .. import __version__
from ..backends import cdp
from ..core.node import Node
from ..core.selector import Filter, Selector, Step, escape_wildcards
from ..driver import Driver
from ..service import IS_WIN, Service
from .overlay import PRIMARY, SECONDARY, Highlighter
from .widgets import SelectorHighlighter, UiBridge

OPS = ["=", "*=", "^=", "$=", "~=", "?=", "!="]
NODE_ROLE = Qt.UserRole
LOADED_ROLE = Qt.UserRole + 1
MAX_CHILDREN_SHOWN = 3000
PLUGIN_DOCS = "https://github.com/KaramBelmoujoud/Spectra-Inspector/blob/main/docs/plugins.md"
PICK_HINT = "Click (or press Ctrl) to capture  ·  Esc or right-click to cancel"


class StepModel:
    def __init__(self, node: Node, role: str, attrs: dict, label: str, suggested: List[str]):
        self.node, self.role, self.attrs, self.label = node, role, attrs, label
        self.include = False
        self.use_role = True
        self.checked = set(suggested)
        self.ops = {}
        self.ci = set()
        self.values = {k: escape_wildcards(str(v)) for k, v in attrs.items()}  # real * / ? stay exact
        self.nth: Optional[int] = None

    def to_step(self, deep: bool) -> Step:
        filters = []
        for name in self.values:
            if name in self.checked:
                filters.append(Filter(name, self.ops.get(name, "="), self.values[name], name in self.ci))
        return Step(role=self.role if self.use_role else "*", filters=filters, nth=self.nth, deep=deep)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Spectra Inspector {__version__}")
        self.resize(1500, 900)
        self.ui = UiBridge()
        self.service = Service()
        self.driver = Driver(init=self.service.init)
        self.driver.start()  # back ends start up on the driver thread while the window opens
        self.hl = Highlighter()
        self.current: Optional[Node] = None
        self.tech = "uia"
        self.steps: List[StepModel] = []
        self.cur_step = -1
        self._suppress = False
        self._suppress_text = False
        self._select_seq = 0
        self.picking = False
        self.hover_node: Optional[Node] = None
        self._last_pos = None
        self._last_focus_key = None
        self.root_items = {}

        self._build_ui()
        self._build_toolbar()
        self._build_menus()
        from ..plugin_manager import PluginManager
        from .plugin_host import GuiHost
        self.plugins = PluginManager(GuiHost(self))
        self.plugins.start()
        self.pick_timer = QTimer(self, interval=30, timeout=self._pick_tick)
        self.focus_timer = QTimer(self, interval=700, timeout=self._focus_tick)
        # live validation only updates the status line; frames are drawn on Validate / Highlight
        self.validate_timer = QTimer(self, singleShot=True, interval=500,
                                     timeout=lambda: self.validate(highlight=False))
        self._pending_tree_node = None
        self.tree_select_timer = QTimer(self, singleShot=True, interval=150, timeout=self._tree_select_now)
        self.hl_timer = QTimer(self, singleShot=True, interval=2500, timeout=self.hl.hide)

        self.driver.submit(lambda: None, self.ui.wrap(lambda _: self._after_init()))  # runs once init is done
        self._restore()
        self.load_roots()
        self.driver.submit(lambda: self.service.web.discover(), self.ui.wrap(self._after_discover))
        self.statusBar().showMessage("Ready. Press F3 (or the target button) to pick an element on screen.")

    # ================================================================== layout
    def _build_ui(self):
        mono = QFont("Cascadia Mono" if IS_WIN else "Monospace", 10)
        mono.setStyleHint(QFont.Monospace)

        # ---- tree (left)
        tree_box = QWidget()
        tl = QVBoxLayout(tree_box)
        tl.setContentsMargins(4, 4, 4, 4)
        self.tree_filter = QLineEdit(placeholderText="Filter loaded elements…")
        self.tree_filter.textChanged.connect(self._filter_tree)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemExpanded.connect(self._on_expand)
        self.tree.currentItemChanged.connect(self._on_tree_select)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        tl.addWidget(self.tree_filter)
        tl.addWidget(self.tree)
        dock = QDockWidget("UI tree", self, objectName="tree_dock")
        dock.setWidget(tree_box)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # ---- properties (right)
        prop_box = QWidget()
        pl = QVBoxLayout(prop_box)
        pl.setContentsMargins(4, 4, 4, 4)
        self.prop_filter = QLineEdit(placeholderText="Filter properties…")
        self.prop_filter.textChanged.connect(self._filter_props)
        self.props = QTableWidget(0, 3)
        self.props.setHorizontalHeaderLabels(["Group", "Property", "Value"])
        self.props.verticalHeader().setVisible(False)
        self.props.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.props.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.props.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.props.setAlternatingRowColors(True)
        self.props.setWordWrap(False)
        self.props.setContextMenuPolicy(Qt.CustomContextMenu)
        self.props.customContextMenuRequested.connect(self._props_menu)
        pl.addWidget(self.prop_filter)
        pl.addWidget(self.props)
        pdock = QDockWidget("Properties", self, objectName="props_dock")
        pdock.setWidget(prop_box)
        self.addDockWidget(Qt.RightDockWidgetArea, pdock)

        # ---- selector editor (center)
        center = QWidget()
        cl = QVBoxLayout(center)
        cl.setContentsMargins(6, 6, 6, 6)
        self.elem_label = QLabel("No element selected")
        self.elem_label.setStyleSheet("font-size: 15px; font-weight: bold; padding: 2px 0 6px 0;")
        self.elem_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.elem_label.setWordWrap(True)  # a long element label must not widen the whole window
        cl.addWidget(self.elem_label)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("Ancestry (tick the levels to use as steps)"))
        self.step_list = QListWidget()
        self.step_list.currentRowChanged.connect(self._on_step_row)
        self.step_list.itemChanged.connect(self._on_step_check)
        ll.addWidget(self.step_list)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Attributes of the selected level"))
        hdr.addStretch(1)
        self.role_check = QCheckBox("Match role")
        self.role_check.toggled.connect(self._on_role_toggle)
        hdr.addWidget(self.role_check)
        hdr.addWidget(QLabel("Index"))
        self.nth_spin = QSpinBox(minimum=0, maximum=9999, specialValueText="none")
        self.nth_spin.setValue(0)
        self.nth_spin.setToolTip(":nth(N), 1 = first match. Type :nth(-1) in the selector for the last match")
        self.nth_spin.valueChanged.connect(self._on_nth)
        hdr.addWidget(self.nth_spin)
        rl.addLayout(hdr)
        self.attr_table = QTableWidget(0, 4)
        self.attr_table.setHorizontalHeaderLabels(["Use", "Attribute", "Match", "Value"])
        self.attr_table.verticalHeader().setVisible(False)
        self.attr_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.attr_table.setColumnWidth(0, 44)
        self.attr_table.setColumnWidth(1, 130)
        self.attr_table.setColumnWidth(2, 70)
        self.attr_table.itemChanged.connect(self._on_attr_item)
        rl.addWidget(self.attr_table)
        split.addWidget(right)
        split.setSizes([330, 520])

        vsplit = QSplitter(Qt.Vertical)
        vsplit.addWidget(split)

        sel_box = QWidget()
        sl = QVBoxLayout(sel_box)
        sl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Selector"))
        row.addStretch(1)
        self.live_check = QCheckBox("Live validation", checked=True)
        row.addWidget(self.live_check)
        sl.addLayout(row)
        self.sel_text = QPlainTextEdit()
        self.sel_text.setFont(mono)
        self.sel_text.setPlaceholderText(
            'uia:Window[process="notepad.exe"] >> Text[name="Email"] /below/ Edit')
        self._hl = SelectorHighlighter(self.sel_text.document())
        self.sel_text.textChanged.connect(self._on_text_edited)
        sl.addWidget(self.sel_text)
        brow = QHBoxLayout()
        b = QPushButton("Validate", objectName="primary")
        b.clicked.connect(lambda: self.validate(highlight=True))
        brow.addWidget(b)
        for text, fn in (("Highlight matches", self.highlight_matches), ("Regenerate", self.regenerate),
                         ("Copy", self.copy_selector), ("Repair…", self.repair)):
            bb = QPushButton(text)
            bb.clicked.connect(fn)
            brow.addWidget(bb)
        self.valid_label = QLabel("")
        self.valid_label.setWordWrap(True)  # long status text wraps instead of widening the window
        self.valid_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        brow.addWidget(self.valid_label, 1)
        sl.addLayout(brow)
        vsplit.addWidget(sel_box)
        vsplit.setSizes([380, 200])
        cl.addWidget(vsplit, 1)
        self.setCentralWidget(center)

        # ---- bottom tabs
        self.tabs = QTabWidget()
        self.code = QPlainTextEdit(readOnly=True)
        self.code.setFont(mono)
        self.tabs.addTab(self.code, "Locators && code")
        self.tabs.addTab(self._actions_tab(), "Try it")
        self.logbox = QPlainTextEdit(readOnly=True)
        self.logbox.setFont(mono)
        self.tabs.addTab(self.logbox, "Log")
        bdock = QDockWidget("Output", self, objectName="out_dock")
        bdock.setWidget(self.tabs)
        self.addDockWidget(Qt.BottomDockWidgetArea, bdock)
        self.resizeDocks([dock, pdock], [380, 430], Qt.Horizontal)
        self.resizeDocks([bdock], [220], Qt.Vertical)

    def _actions_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row1 = QHBoxLayout()
        self.action_buttons = {}
        for name in ("Highlight", "Bring to front", "Focus", "Click", "Invoke", "Toggle", "Expand", "Collapse",
                     "Select", "Scroll into view", "Screenshot…"):
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, n=name: self.run_action(n))
            row1.addWidget(b)
            self.action_buttons[name] = b
        row1.addStretch(1)
        lay.addLayout(row1)
        row2 = QHBoxLayout()
        self.value_edit = QLineEdit(placeholderText="Text for Set value / Type text")
        row2.addWidget(self.value_edit, 1)
        for name in ("Set value", "Type text"):
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, n=name: self.run_action(n))
            row2.addWidget(b)
        lay.addLayout(row2)
        self.plugin_actions_row = QHBoxLayout()
        lay.addLayout(self.plugin_actions_row)
        note = QLabel("These act on the real application. Click and Type text move the mouse / send keys.")
        note.setStyleSheet("color: #8a8d94;")
        lay.addWidget(note)
        lay.addStretch(1)
        return w

    def _build_toolbar(self):
        tb = self.addToolBar("Main")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        self.pick_act = QAction("🎯  Pick element", self, checkable=True, shortcut=QKeySequence("F3"))
        self.pick_act.setToolTip("Hover any element, then click it (or press Ctrl) to capture it. The click is not "
                                 "passed to the application. Esc or right-click cancels. F3")
        self.pick_act.toggled.connect(self.toggle_pick)
        tb.addAction(self.pick_act)
        self.minimize_act = QAction("🗕  Minimize while picking", self, checkable=True)
        self.minimize_act.setToolTip("Get Spectra out of the way while you pick an element; it comes back "
                                     "once you capture or cancel")
        self.minimize_act.setChecked(QSettings("Spectra", "Inspector").value("minimize_on_pick", True, type=bool))
        self.minimize_act.toggled.connect(lambda on: QSettings("Spectra", "Inspector").setValue("minimize_on_pick", on))
        tb.addAction(self.minimize_act)
        a = QAction("🌐  Pick in browser tab", self, shortcut=QKeySequence("F4"))
        a.setToolTip("Uses the browser's own inspect mode over DevTools: click an element in the page. F4")
        a.triggered.connect(self.pick_in_browser)
        tb.addAction(a)
        self.focus_act = QAction("👁  Follow focus", self, checkable=True)
        self.focus_act.toggled.connect(lambda on: self.focus_timer.start() if on else self.focus_timer.stop())
        tb.addAction(self.focus_act)
        tb.addSeparator()
        a = QAction("⟳  Refresh tree", self, shortcut=QKeySequence("F5"))
        a.triggered.connect(self.refresh_tree)
        tb.addAction(a)
        self.raw_act = QAction("Raw view", self, checkable=True)
        self.raw_act.setToolTip("Show the UI Automation raw view (every element) instead of the control view")
        self.raw_act.toggled.connect(self.toggle_raw)
        tb.addAction(self.raw_act)
        tb.addSeparator()

        browser_btn = QToolButton()
        browser_btn.setText("🧭  Browsers")
        browser_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(browser_btn)
        for name, exe in cdp.installed_browsers().items():
            act = menu.addAction(f"Launch {name} with inspection enabled")
            act.triggered.connect(lambda _=False, e=exe, n=name: self.launch_browser(n, e))
        if menu.actions():
            menu.addSeparator()
        menu.addAction("Connect to debugging port…", self.connect_port)
        menu.addAction("Scan common ports", self.discover)
        menu.addSeparator()
        self.web_pick_act = menu.addAction("Hover-pick inside connected browsers")
        self.web_pick_act.setCheckable(True)
        self.web_pick_act.setChecked(True)
        self.web_pick_act.toggled.connect(lambda on: setattr(self.service, "prefer_web", on))
        menu.addAction("How browser inspection works", self.browser_help)
        browser_btn.setMenu(menu)
        tb.addWidget(browser_btn)

        self.java_act = QAction("☕  Java", self, checkable=True, checked=True)
        self.java_act.setToolTip("Prefer Java Access Bridge when hovering Java windows")
        self.java_act.toggled.connect(lambda on: setattr(self.service, "prefer_java", on))
        tb.addAction(self.java_act)
        tb.addSeparator()
        a = QAction("Open selector…", self, shortcut=QKeySequence.Open)
        a.triggered.connect(self.open_selector)
        tb.addAction(a)
        a = QAction("Save selector…", self, shortcut=QKeySequence.Save)
        a.triggered.connect(self.save_selector)
        tb.addAction(a)
        a = QAction("Syntax help", self, shortcut=QKeySequence("F1"))
        a.triggered.connect(self.syntax_help)
        tb.addAction(a)

    # ================================================================== helpers
    def _after_init(self):
        if self.driver.init_error:
            self.log("Driver start-up error:\n" + self.driver.init_error)
        for tech, err in self.service.errors.items():
            self.log(f"[{tech}] not available: {err}")
        self.plugins.emit("app_started")

    def log(self, text: str):
        self.logbox.appendPlainText(time.strftime("%H:%M:%S  ") + text)

    def _err(self, prefix):
        def handler(e, tb):
            self.ui.post(lambda: (self.log(f"{prefix}: {e}"), self.statusBar().showMessage(f"{prefix}: {e}", 8000)))
        return handler

    def _restore(self):
        s = QSettings("Spectra", "Inspector")
        if s.value("geometry"):
            self.restoreGeometry(s.value("geometry"))
        if s.value("state"):
            self.restoreState(s.value("state"))

    def closeEvent(self, ev):
        self.plugins.emit("app_closing")
        s = QSettings("Spectra", "Inspector")
        s.setValue("geometry", self.saveGeometry())
        s.setValue("state", self.saveState())
        self.pick_timer.stop()
        self._catcher = None
        if IS_WIN:
            from ..backends import win32
            win32.remove_all_hooks()  # never leave a global mouse hook behind
        self.hl.hide()
        self.plugins.stop()
        super().closeEvent(ev)

    # ================================================================== tree
    def load_roots(self):
        def job():
            return [(r, r.label()) for r in self.service.roots()]

        def done(items):
            self.tree.clear()
            self.root_items = {}
            for node, label in items:
                it = QTreeWidgetItem([label])
                it.setData(0, NODE_ROLE, node)
                it.setData(0, LOADED_ROLE, False)
                it.addChild(QTreeWidgetItem(["…"]))
                f = it.font(0)
                f.setBold(True)
                it.setFont(0, f)
                self.tree.addTopLevelItem(it)
                self.root_items[node.tech] = it
            first = self.tree.topLevelItem(0)
            if first:
                first.setExpanded(True)
        self.driver.submit(job, self.ui.wrap(done), self._err("Loading tree"))

    def _on_expand(self, item: QTreeWidgetItem):
        if item.data(0, LOADED_ROLE):
            return
        node = item.data(0, NODE_ROLE)
        if node is None:
            return
        item.setData(0, LOADED_ROLE, True)

        def job():
            kids = node.children()
            return [(k, k.label()) for k in kids[:MAX_CHILDREN_SHOWN]], len(kids)

        def done(res):
            kids, total = res
            self._fill(item, kids, total)
        self.driver.submit(job, self.ui.wrap(done), self._err("Expanding"))

    def _fill(self, item: QTreeWidgetItem, kids, total):
        if not isValid(item):  # the tree was reloaded while the children were being read
            return
        item.takeChildren()
        for node, label in kids:
            ch = QTreeWidgetItem([label])
            ch.setData(0, NODE_ROLE, node)
            ch.setData(0, LOADED_ROLE, False)
            ch.addChild(QTreeWidgetItem(["…"]))
            item.addChild(ch)
        if total > len(kids):
            item.addChild(QTreeWidgetItem([f"… {total - len(kids)} more not shown"]))
        item.setData(0, LOADED_ROLE, True)
        if not kids:
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)

    def _on_tree_select(self, cur, _prev):
        if self._suppress or cur is None:
            return
        node = cur.data(0, NODE_ROLE)
        if node is not None and not node.is_root():
            # arrow-keying through the tree fires this for every row: only load
            # the row the user settles on
            self._pending_tree_node = node
            self.tree_select_timer.start()

    def _tree_select_now(self):
        node, self._pending_tree_node = self._pending_tree_node, None
        if node is not None:
            self.select_node(node, reveal=False)

    def _filter_tree(self, text):
        text = text.casefold()

        def visit(item) -> bool:
            any_child = False
            for i in range(item.childCount()):
                if visit(item.child(i)):
                    any_child = True
            match = not text or text in item.text(0).casefold()
            item.setHidden(not (match or any_child))
            return match or any_child

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def refresh_tree(self):
        def job():
            self.service.web.refresh(force=True)
            for p in self.service.web.pages():
                p._dirty = True
            if self.service.uia is not None:
                self.service.uia.set_view(self.service.uia.raw_view)
        self.driver.submit(job, self.ui.wrap(lambda _: self.load_roots()), self._err("Refresh"))

    def toggle_raw(self, on):
        if self.service.uia is None:
            return
        self.driver.submit(lambda: self.service.uia.set_view(on), self.ui.wrap(lambda _: self.load_roots()))

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item or item.data(0, NODE_ROLE) is None:
            return
        node = item.data(0, NODE_ROLE)
        m = QMenu(self)
        m.addAction("Highlight", lambda: self._flash(node))
        m.addAction("Reload children", lambda: self._reload_item(item))
        m.addAction("Copy label", lambda: QGuiApplication.clipboard().setText(item.text(0)))
        if node.tech == "web":
            m.addAction("Pick in this tab (browser inspect mode)", lambda: self._start_page_inspect(node))
        actions = self.plugins.registry.of("actions")
        if actions:
            sub = m.addMenu("Plugin actions")
            for act in actions:
                sub.addAction(act["name"], lambda a=act, n=node: self._run_plugin_action(a, n))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _reload_item(self, item):
        node = item.data(0, NODE_ROLE)

        def job():
            if hasattr(node, "refresh"):
                node.refresh()
            if hasattr(node, "page"):
                node.page._dirty = True
            if hasattr(node, "_kids") and node.tech == "java":
                node._kids = None
        self.driver.submit(job, self.ui.wrap(lambda _: (item.setData(0, LOADED_ROLE, False),
                                                        self._on_expand(item), item.setExpanded(True))))

    def reveal(self, chain: List[Node]):
        """Expand the tree down to the element (loading levels as needed)."""
        if not chain:
            return
        root_item = self.root_items.get(chain[0].tech)
        if root_item is None:
            return
        root_node = root_item.data(0, NODE_ROLE)

        def job():
            levels = []
            parent = root_node
            for target in chain:
                kids = parent.children()
                tk = target.key()
                idx = next((i for i, k in enumerate(kids) if k.key() == tk), -1)
                levels.append(([(k, k.label()) for k in kids[:MAX_CHILDREN_SHOWN]], len(kids), idx))
                if idx < 0:
                    break
                parent = kids[idx]
            return levels

        def done(levels):
            if not isValid(root_item):  # the tree was reloaded meanwhile
                return
            self._suppress = True
            try:
                item = root_item
                for kids, total, idx in levels:
                    if not item.data(0, LOADED_ROLE) or item.childCount() != min(total, MAX_CHILDREN_SHOWN) + \
                            (1 if total > MAX_CHILDREN_SHOWN else 0):
                        self._fill(item, kids, total)
                    item.setExpanded(True)
                    if idx < 0 or idx >= item.childCount():
                        break
                    item = item.child(idx)
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            finally:
                self._suppress = False
        self.driver.submit(job, self.ui.wrap(done), self._err("Reveal in tree"))

    # ================================================================== selection
    def select_node(self, node: Node, reveal: bool = True, flash: bool = True):
        self._select_seq += 1
        seq = self._select_seq
        self.current = node
        self.elem_label.setText("Loading…")

        def gather():
            chain = node.ancestry()
            data = [(n, n.role(), dict(n.selector_attrs()), n.label(), n.suggested_attrs(is_top=(i == 0)))
                    for i, n in enumerate(chain)]
            props = list(node.properties()) + self.plugins.extra_properties(node)
            rect = node.rect()
            return chain, data, props, rect, node.label()

        def gathered(res):
            if seq != self._select_seq:
                return
            chain, data, props, rect, label = res
            self.tech = node.tech
            self.elem_label.setText(f"[{node.tech}]  {label}")
            self._fill_props(props)
            self.steps = [StepModel(*d) for d in data]
            if self.steps:
                self.steps[-1].include = True
                self.steps[0].include = True
            self._render_steps(select=len(self.steps) - 1)
            self._render_selector()
            if flash and rect:
                self._show_rect(rect)
            if node.tech == "web":
                self.driver.submit(lambda: node.page.highlight(node.bid) if hasattr(node, "bid") else None)
            if reveal:
                self.reveal(chain)
            self._generate(node, seq)
            self.plugins.emit("element_selected", node)

        self.driver.submit(gather, self.ui.wrap(gathered), self._err("Reading element"), coalesce="select")

    def _generate(self, node: Node, seq: int):
        self.valid_label.setText("Generating selector…")
        self.valid_label.setObjectName("")

        def job():
            if seq != self._select_seq:
                return None  # the user already moved on to another element
            sel, res = self.service.engine.generate(node)
            return sel, res, self._code_for(node, sel)

        def done(r):
            if seq != self._select_seq:
                return
            sel, res, code = r
            # A relative selector names a landmark that is not an ancestor, so the
            # ancestry editor cannot rebuild it. Show the generated text as-is.
            if any(st.relation for st in sel.steps):
                self._suppress_text = True
                self.sel_text.setPlainText(sel.to_text())
                self._suppress_text = False
                self.statusBar().showMessage(
                    "Anchored to a nearby element so this selector survives a reorder.", 8000)
            else:
                inc = sel.included or []
                for i, st in enumerate(self.steps):
                    st.include = i in inc
                for pos, idx in enumerate(inc):
                    if idx >= len(self.steps) or pos >= len(sel.steps):
                        continue
                    gst, m = sel.steps[pos], self.steps[idx]
                    m.checked = {f.name for f in gst.filters}
                    for f in gst.filters:
                        m.ops[f.name] = f.op or "="
                        m.values[f.name] = f.value
                    m.nth = gst.nth
                    m.use_role = gst.role != "*"
                self._render_steps(select=len(self.steps) - 1)
                self._render_selector(validate=False)
            self._show_result(res)
            self.code.setPlainText(code)
            self.plugins.emit("selector_validated", sel.to_text(), res)
        self.driver.submit(job, self.ui.wrap(done), self._err("Generating selector"), coalesce="generate")

    def regenerate(self):
        if self.current is not None:
            self._generate(self.current, self._select_seq)

    def _fill_props(self, rows):
        self.props.setRowCount(0)
        self.props.setRowCount(len(rows))
        for r, (g, k, v) in enumerate(rows):
            for c, text in enumerate((g, k, v)):
                it = QTableWidgetItem(str(text))
                it.setToolTip(str(text)[:2000])
                self.props.setItem(r, c, it)
        self.props.resizeColumnToContents(0)
        self.props.resizeColumnToContents(1)
        self._filter_props(self.prop_filter.text())

    def _filter_props(self, text):
        t = text.casefold()
        for r in range(self.props.rowCount()):
            row_text = " ".join(self.props.item(r, c).text() for c in range(3) if self.props.item(r, c)).casefold()
            self.props.setRowHidden(r, bool(t) and t not in row_text)

    def _props_menu(self, pos):
        it = self.props.itemAt(pos)
        if not it:
            return
        r = it.row()
        k, v = self.props.item(r, 1).text(), self.props.item(r, 2).text()
        m = QMenu(self)
        m.addAction("Copy value", lambda: QGuiApplication.clipboard().setText(v))
        m.addAction("Copy name = value", lambda: QGuiApplication.clipboard().setText(f"{k} = {v}"))
        m.addAction("Copy all properties as JSON", self._copy_props_json)
        m.exec(self.props.viewport().mapToGlobal(pos))

    def _copy_props_json(self):
        out = {}
        for r in range(self.props.rowCount()):
            g, k, v = (self.props.item(r, c).text() for c in range(3))
            out.setdefault(g, {})[k] = v
        QGuiApplication.clipboard().setText(json.dumps(out, indent=2, ensure_ascii=False))

    # ================================================================== selector editor
    def _render_steps(self, select: int = -1):
        self._suppress = True
        self.step_list.clear()
        for i, st in enumerate(self.steps):
            it = QListWidgetItem(("    " * min(i, 8)) + st.label)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if st.include else Qt.Unchecked)
            self.step_list.addItem(it)
        self._suppress = False
        if select >= 0:
            self.step_list.setCurrentRow(select)

    def _on_step_check(self, item):
        if self._suppress:
            return
        i = self.step_list.row(item)
        self.steps[i].include = item.checkState() == Qt.Checked
        self._render_selector()

    def _on_step_row(self, row):
        self.cur_step = row
        self._render_attrs()

    def _render_attrs(self):
        self._suppress = True
        self.attr_table.setRowCount(0)
        if not (0 <= self.cur_step < len(self.steps)):
            self._suppress = False
            return
        st = self.steps[self.cur_step]
        self.role_check.setText(f"Match role  ({st.role})")
        self.role_check.setChecked(st.use_role)
        self.nth_spin.setValue(max(0, st.nth or 0))
        names = list(st.values.keys())
        self.attr_table.setRowCount(len(names))
        for r, name in enumerate(names):
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            use.setCheckState(Qt.Checked if name in st.checked else Qt.Unchecked)
            self.attr_table.setItem(r, 0, use)
            n = QTableWidgetItem(name)
            n.setFlags(Qt.ItemIsEnabled)
            self.attr_table.setItem(r, 1, n)
            combo = QComboBox()
            combo.addItems(OPS + [o + " i" for o in OPS[:-1]])
            op = st.ops.get(name, "=") + (" i" if name in st.ci else "")
            combo.setCurrentText(op)
            combo.currentTextChanged.connect(lambda txt, nm=name: self._on_op(nm, txt))
            self.attr_table.setCellWidget(r, 2, combo)
            v = QTableWidgetItem(st.values[name])
            v.setToolTip(st.attrs.get(name, ""))
            self.attr_table.setItem(r, 3, v)
        self._suppress = False

    def _on_attr_item(self, item):
        if self._suppress or not (0 <= self.cur_step < len(self.steps)):
            return
        st = self.steps[self.cur_step]
        name = self.attr_table.item(item.row(), 1).text()
        if item.column() == 0:
            if item.checkState() == Qt.Checked:
                st.checked.add(name)
                if not st.include:
                    st.include = True
                    self._suppress = True
                    self.step_list.item(self.cur_step).setCheckState(Qt.Checked)
                    self._suppress = False
            else:
                st.checked.discard(name)
        elif item.column() == 3:
            st.values[name] = item.text()
            st.checked.add(name)
            self._suppress = True
            self.attr_table.item(item.row(), 0).setCheckState(Qt.Checked)
            self._suppress = False
        self._render_selector()

    def _on_op(self, name, txt):
        if not (0 <= self.cur_step < len(self.steps)):
            return
        st = self.steps[self.cur_step]
        ci = txt.endswith(" i")
        st.ops[name] = txt.replace(" i", "")
        (st.ci.add if ci else st.ci.discard)(name)
        st.checked.add(name)
        self._render_attrs()
        self._render_selector()

    def _on_role_toggle(self, on):
        if self._suppress or not (0 <= self.cur_step < len(self.steps)):
            return
        self.steps[self.cur_step].use_role = on
        self._render_selector()

    def _on_nth(self, v):
        if self._suppress or not (0 <= self.cur_step < len(self.steps)):
            return
        self.steps[self.cur_step].nth = v or None
        self._render_selector()

    def build_selector(self) -> Optional[Selector]:
        steps = []
        prev = None
        for i, st in enumerate(self.steps):
            if not st.include:
                continue
            deep = (i != 0) if prev is None else (i != prev + 1)
            steps.append(st.to_step(deep))
            prev = i
        if not steps:
            return None
        steps[0].tech = self.tech
        return Selector(steps)

    def _render_selector(self, validate: bool = True):
        sel = self.build_selector()
        self._suppress_text = True
        self.sel_text.setPlainText(sel.to_text() if sel else "")
        self._suppress_text = False
        if validate and self.live_check.isChecked():
            self.validate_timer.start()

    def _on_text_edited(self):
        if getattr(self, "_suppress_text", False):
            return
        if self.live_check.isChecked():
            self.validate_timer.start()

    def selector_text(self) -> str:
        return " ".join(self.sel_text.toPlainText().split("\n")).strip()

    def validate(self, highlight: bool = True):
        text = self.selector_text()
        if not text:
            return
        self.valid_label.setText("Validating…")

        def job():
            res = self.service.engine.find_all(text, timeout=10)
            rects = [m.rect() for m in res.matches[:30]] if highlight else []
            return res, rects

        def done(r):
            res, rects = r
            self._show_result(res)
            self.plugins.emit("selector_validated", text, res)
            if highlight and rects:
                cur_key = self.current.key() if self.current is not None else None
                items = []
                for m, rc in zip(res.matches, rects):
                    items.append((rc, PRIMARY if m.key() == cur_key else SECONDARY))
                self.hl.show_many(items)
                self.hl_timer.start()
        self.driver.submit(job, self.ui.wrap(done), self._err("Validation"), coalesce="validate")

    def _show_result(self, res):
        n = len(res.matches)
        if res.error:
            txt, style = f"✖  {res.error}", "status_bad"
        elif n == 1:
            same = self.current is not None and res.matches[0] == self.current
            txt = "✔  Unique match" + ("" if same or self.current is None else " (a different element than the selected one)")
            style = "status_ok" if same or self.current is None else "status_warn"
        elif n == 0:
            failed = next((i for i, c in enumerate(res.step_counts) if c == 0), len(res.step_counts) - 1)
            txt, style = f"✖  No match (step {failed + 1} found nothing)", "status_bad"
        else:
            txt, style = f"⚠  {n} matches - add attributes or an index", "status_warn"
        txt += f"   ·  {res.elapsed_ms:.0f} ms, {res.visited} elements visited"
        try:
            from ..core.resilience import grade
            g = grade(self.selector_text())
        except Exception:  # noqa: BLE001 - a syntax error is already reported above
            g = None
        if g is not None:
            txt += f"   ·  {g.band} {g.score}"
            self.valid_label.setToolTip("\n".join(g.reasons))
        else:
            self.valid_label.setToolTip("")
        self.valid_label.setText(txt)
        self.valid_label.setObjectName(style)
        self.valid_label.style().unpolish(self.valid_label)
        self.valid_label.style().polish(self.valid_label)

    def highlight_matches(self):
        self.validate(highlight=True)

    def copy_selector(self):
        QGuiApplication.clipboard().setText(self.selector_text())
        self.statusBar().showMessage("Selector copied", 2500)

    def repair(self):
        """Find the element for a broken selector by relaxing it step by step."""
        text = self.selector_text()
        if not text:
            return

        def job():
            from ..core.selector import parse
            sel = parse(text)
            for si in range(len(sel.steps) - 1, -1, -1):
                st = sel.steps[si]
                for fi in range(len(st.filters) - 1, -1, -1):
                    st2 = Selector([Step(s.role, list(s.filters), s.nth, s.visible, s.deep, s.tech, s.relation)
                                    for s in sel.steps])
                    st2.steps[si].filters.pop(fi)
                    res = self.service.engine.find_all(st2, timeout=5)
                    if len(res.matches) == 1:
                        return st2.to_text(), res, ""
            hit = self.service.engine.heal(text, hint=self.current, timeout=6)
            if hit is None:
                return None, None, ""
            res = self.service.engine.find_all(hit.selector, timeout=5)
            if len(res.matches) != 1:
                return None, None, ""
            note = f"Self-healed ({hit.confidence:.0%}). {hit.why}"
            return hit.selector, res, note

        def done(r):
            fixed, res, note = r
            if not fixed:
                QMessageBox.information(self, "Repair", "Could not repair this selector by relaxing it or by "
                                        "matching a similar element. Pick the element again.")
                return
            if note:
                self.statusBar().showMessage(note, 8000)
            self._suppress_text = True
            self.sel_text.setPlainText(fixed)
            self._suppress_text = False
            self._show_result(res)
            self.select_node(res.matches[0], reveal=True)
        self.driver.submit(job, self.ui.wrap(done), self._err("Repair"))

    # ================================================================== picking
    def toggle_pick(self, on):
        if not IS_WIN:
            self.statusBar().showMessage("On-screen picking needs Windows; use 'Pick in browser tab' instead.", 6000)
            self.pick_act.setChecked(False)
            return
        self.picking = on
        self.hover_node = None
        self.hover_rect = None
        self._last_pos = None
        if on:
            self.statusBar().showMessage(PICK_HINT)
            if self.minimize_act.isChecked() and not self.isMinimized():
                self._state_before_pick = self.windowState()
                self.showMinimized()
            self._start_click_catcher()
            self.pick_timer.start()
            self.plugins.emit("pick_started")
        else:
            self._stop_click_catcher()
            self.pick_timer.stop()
            self.hl.hide()
            self._restore_after_pick()
            self.statusBar().showMessage("Picking cancelled", 2000)
            self.plugins.emit("pick_finished", None)

    # ---- click to capture: a global mouse hook swallows the click so the target app never gets it
    def _start_click_catcher(self):
        from ..backends import win32
        self._stop_click_catcher()

        def clicked(button, x, y):  # runs inside the hook: only schedule work
            QTimer.singleShot(0, lambda: self._on_pick_click(button, x, y))

        catcher = win32.ClickCatcher(clicked, on_released=lambda: QTimer.singleShot(0, lambda: catcher.uninstall()))
        if catcher.install():
            self._catcher = catcher
        else:
            self.log("Could not install the mouse hook; use Ctrl to capture")

    def _stop_click_catcher(self):
        catcher, self._catcher = getattr(self, "_catcher", None), None
        if catcher is not None and not catcher.stop():
            QTimer.singleShot(2000, lambda: catcher.uninstall())  # safety net if the release never arrives

    def _on_pick_click(self, button, x, y):
        if not self.picking:
            return
        if button == "right":
            self.pick_act.setChecked(False)  # right click cancels
            return
        r = self.hover_rect
        if self.hover_node is not None and r and r[0] <= x < r[2] and r[1] <= y < r[3]:
            self._capture(self.hover_node)
            return
        self._stop_click_catcher()  # hover not there yet: look the point up, then capture

        def done(n):
            if self.picking and n is not None:
                self._capture(n)
            elif self.picking:
                self._start_click_catcher()
        self.driver.submit(lambda: self.service.element_at(x, y), self.ui.wrap(done), None)

    def _capture(self, node):
        self._stop_click_catcher()
        self.pick_timer.stop()
        self.picking = False
        self.pick_act.blockSignals(True)
        self.pick_act.setChecked(False)
        self.pick_act.blockSignals(False)
        self._restore_after_pick()
        self.statusBar().showMessage(f"Captured {node.label()}", 4000)
        self.select_node(node)
        self.plugins.emit("pick_finished", node)

    def _restore_after_pick(self):
        state = getattr(self, "_state_before_pick", None)
        if state is None:
            return
        self._state_before_pick = None
        self.setWindowState(state & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        if IS_WIN:  # Qt alone only flashes the taskbar button when another app has focus
            from ..backends import win32
            win32.bring_to_front(int(self.winId()))

    def _pick_tick(self):
        from ..backends import win32
        if win32.key_down(win32.VK_ESCAPE):
            self.pick_act.setChecked(False)
            return
        if win32.key_down(win32.VK_CONTROL) and self.hover_node is not None:
            self._capture(self.hover_node)
            return
        pos = win32.cursor_pos()
        if pos == self._last_pos:
            return
        self._last_pos = pos

        prev = self.hover_node

        def job():
            n = self.service.element_at(*pos)
            if n is None:
                return None
            if prev is not None and n == prev:
                return prev, None, None  # same element as before: nothing to redraw
            return n, n.rect(), n.label()

        def done(r):
            if not self.picking:
                return
            if r is None:
                return
            node, rect, label = r
            if label is None:
                return
            self.hover_node, self.hover_rect = node, rect
            self.hl.show(rect, PRIMARY)
            self.statusBar().showMessage(f"[{node.tech}] {label}    -  {PICK_HINT}")
        self.driver.submit(job, self.ui.wrap(done), None, coalesce="hover")

    def _focus_tick(self):
        if self.service.uia is None:
            return

        def job():
            n = self.service.uia.focused()
            if n is None:
                return None
            from ..backends.uia import P_PID
            if n.prop(P_PID) == os.getpid():
                return None
            return n, n.key()

        def done(r):
            if r and r[1] != self._last_focus_key:
                self._last_focus_key = r[1]
                self.select_node(r[0])
        self.driver.submit(job, self.ui.wrap(done), None, coalesce="focus")

    # ================================================================== browsers
    def _after_discover(self, eps):
        if eps:
            self.log("Connected to browser debugging endpoints: " +
                     ", ".join(f"{e.browser or 'browser'} on port {e.port}" for e in eps))
            self.load_roots()

    def discover(self):
        self.driver.submit(lambda: self.service.web.discover(),
                           self.ui.wrap(lambda eps: (self._after_discover(eps), None if eps else
                                        self.statusBar().showMessage("No debugging port found on 9222-9225, "
                                                                     "9229, 9333", 5000))))

    def connect_port(self):
        txt, ok = QInputDialog.getText(self, "Connect to browser", "Debugging endpoint (host:port or port):",
                                       text="127.0.0.1:9222")
        if not ok or not txt.strip():
            return
        host, _, port = txt.strip().rpartition(":")
        host = host or "127.0.0.1"
        try:
            port_n = int(port)
        except ValueError:
            QMessageBox.warning(self, "Connect", "Port must be a number")
            return

        def done(ep):
            if ep is None:
                QMessageBox.warning(self, "Connect", f"Nothing answered on {host}:{port_n}. Start the browser with "
                                    f"--remote-debugging-port={port_n} (or use Launch).")
            else:
                self.log(f"Connected to {ep.browser} at {host}:{port_n}")
                self.load_roots()
        self.driver.submit(lambda: self.service.web.add_endpoint(host, port_n), self.ui.wrap(done),
                           self._err("Connect"))

    def launch_browser(self, name, exe):
        port = 9222
        while port < 9300:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    break
            port += 1
        try:
            cdp.launch_browser(exe, port, "https://example.com")
        except OSError as e:
            QMessageBox.warning(self, "Launch", str(e))
            return
        self.log(f"Launched {name} with inspection on port {port} (separate profile, your sign-ins there persist)")

        def job():
            for _ in range(40):
                ep = self.service.web.add_endpoint("127.0.0.1", port)
                if ep:
                    return ep
                time.sleep(0.25)
            return None
        self.driver.submit(job, self.ui.wrap(lambda ep: (self.load_roots(), self.log(
            "Browser ready" if ep else "Browser did not open its debugging port"))))

    def pick_in_browser(self):
        def job():
            pages = self.service.web.pages()
            return [(p, p.title or p.url) for p in pages]

        def done(pages):
            if not pages:
                QMessageBox.information(self, "Pick in browser",
                                        "No browser is connected. Use Browsers > Launch, or start Chrome/Edge with "
                                        "--remote-debugging-port=9222 and use Browsers > Connect.")
                return
            if len(pages) == 1:
                self._start_page_inspect(pages[0][0].tab_node)
                return
            labels = [f"{i + 1}. {t[:90]}" for i, (_, t) in enumerate(pages)]
            choice, ok = QInputDialog.getItem(self, "Pick in browser", "Which tab?", labels, 0, False)
            if ok:
                self._start_page_inspect(pages[labels.index(choice)][0].tab_node)
        self.driver.submit(job, self.ui.wrap(done), self._err("Browser"))

    def _start_page_inspect(self, node):
        page = node.page

        def picked(n):
            self.ui.post(lambda: (self.statusBar().showMessage("Captured element from browser", 3000),
                                  self.select_node(n) if n is not None else None))

        def job():
            page.bring_to_front()
            page.start_inspect(picked)
        self.driver.submit(job, None, self._err("Browser inspect"))
        self.statusBar().showMessage(f"Click an element in the browser tab \"{page.title[:50]}\" (Esc in the "
                                     f"browser cancels)", 15000)

    def browser_help(self):
        QMessageBox.information(self, "Browser inspection without an extension", (
            "Spectra reads web pages in two ways, neither of which needs a browser extension:\n\n"
            "1. DevTools protocol (full DOM). Start Chrome, Edge or Brave from Browsers > Launch, or start it "
            "yourself with --remote-debugging-port=9222 and use Connect. You then get the live DOM including "
            "shadow roots and iframes, in-page highlighting, the browser's own click-to-inspect (F4), CSS/XPath, "
            "and hover-picking with F3.\n\n"
            "2. UI Automation (accessibility tree). Without a debugging port, F3 still shows page elements as "
            "UIA nodes (roles, names, values) because Chromium and Firefox expose their accessibility tree.\n\n"
            "Electron and WebView2 apps work the same way when started with --remote-debugging-port "
            "(WebView2: set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9229)."))

    # ================================================================== actions
    def _flash(self, node: Node):
        self.driver.submit(node.rect, self.ui.wrap(self._show_rect))
        if node.tech == "web" and hasattr(node, "bid"):
            self.driver.submit(lambda: node.page.highlight(node.bid))

    def _show_rect(self, rect):
        if rect:
            self.hl.show(rect, PRIMARY)
            self.hl_timer.start()

    def run_action(self, name: str):
        node = self.current
        if node is None:
            self.statusBar().showMessage("Select an element first", 3000)
            return
        if name == "Highlight":
            self._flash(node)
            return
        if name == "Screenshot…":
            self.screenshot(node)
            return
        arg = self.value_edit.text()

        def job():
            from ..api import Element, _mouse_click, _send_unicode
            if name == "Bring to front":
                if node.tech == "web":
                    node.page.bring_to_front()
                else:
                    from ..backends import win32
                    hw = getattr(node, "hwnd", 0)
                    hw = hw() if callable(hw) else hw
                    cur = node
                    while not hw and cur is not None:
                        cur = cur.parent()
                        if cur is None:
                            break
                        h = getattr(cur, "hwnd", 0)
                        hw = h() if callable(h) else h
                    win32.bring_to_front(win32.root_window(hw) if hw else 0)
                return "done"
            if node.tech == "web":
                el = Element.__new__(Element)
                el.node = node
                if name == "Click":
                    el._web_call("function(){this.scrollIntoView({block:'center'}); this.click();}")
                elif name == "Focus":
                    el._web_call("function(){this.focus();}")
                elif name == "Set value":
                    el._web_call("function(v){this.value=v;this.dispatchEvent(new Event('input',{bubbles:true}));"
                                 "this.dispatchEvent(new Event('change',{bubbles:true}));}", arg)
                elif name == "Type text":
                    el._web_call("function(){this.focus();}")
                    node.page.ensure().send("Input.insertText", {"text": arg})
                elif name == "Scroll into view":
                    el._web_call("function(){this.scrollIntoView({block:'center'});}")
                else:
                    raise RuntimeError(f"{name} is not available for web elements (try Click)")
                return "done"
            if name == "Click":
                r = node.rect()
                if not r:
                    raise RuntimeError("element has no on-screen rectangle")
                _mouse_click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                return "done"
            if name == "Type text":
                if node.tech == "uia":
                    node.el.SetFocus()
                _send_unicode(arg)
                return "done"
            if node.tech == "uia":
                self.service.uia.action(node, name, arg)
                return "done"
            raise RuntimeError(f"{name} is not available for {node.tech} elements")
        self.driver.submit(job, self.ui.wrap(lambda r: self.statusBar().showMessage(f"{name}: {r}", 3000)),
                           self._err(name))

    def screenshot(self, node: Node):
        path, _ = QFileDialog.getSaveFileName(self, "Save element screenshot", "element.png", "PNG (*.png)")
        if not path:
            return

        def job():
            if node.tech == "web":
                import base64
                conn = node.page.ensure()
                conn.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node.bid})
                box = node.page.css_box(node.bid)
                lm = conn.send("Page.getLayoutMetrics")
                vp = lm.get("cssLayoutViewport") or lm.get("layoutViewport", {})
                clip = {"x": box[0] + vp.get("pageX", 0), "y": box[1] + vp.get("pageY", 0),
                        "width": max(1, box[2] - box[0]), "height": max(1, box[3] - box[1]), "scale": 1}
                data = conn.send("Page.captureScreenshot", {"format": "png", "clip": clip,
                                                            "captureBeyondViewport": True})["data"]
                with open(path, "wb") as f:
                    f.write(base64.b64decode(data))
                return path
            r = node.rect()
            if not r:
                raise RuntimeError("element has no on-screen rectangle")
            from PIL import ImageGrab
            ImageGrab.grab(bbox=r, all_screens=True).save(path)
            return path
        self.driver.submit(job, self.ui.wrap(lambda p: self.statusBar().showMessage(f"Saved {p}", 4000)),
                           self._err("Screenshot"))

    # ================================================================== code generation
    def _code_for(self, node: Node, sel: Selector) -> str:
        """Runs on the driver thread. The Spectra selector and API are built in; everything else
        (Playwright, Selenium, Robot Framework, pywinauto, other formats...) comes from plugins."""
        text = sel.to_text()
        try:
            from ..core.resilience import grade
            g = grade(sel)
            quality = [f"# Selector quality: {g.summary()}"] + [f"# {reason}" for reason in g.reasons[:4]]
        except Exception:  # noqa: BLE001
            quality = []
        lines = quality + [
            "", "# Spectra selector", text, "",
            "# Python, with the Spectra API (spectra/api.py)",
            "from spectra.api import Spectra",
            "s = Spectra(" + ("browser_port=%d" % node.page.endpoint.port if node.tech == "web" else "") + ")",
            f"el = s.find(r'''{text}''', timeout=10)",
            "el.click()", ""]
        if node.tech == "web":
            from ..core.webpaths import css_path, xpath
            css, xp = css_path(node), xpath(node)
            lines += ["# CSS", css, "", "# XPath", xp, ""]
            try:
                n_css = node.page.count_css(css) if not css.startswith("/*") else None
                n_xp = node.page.count_xpath(xp) if "/*" not in xp else None
                lines.insert(0, f"# CSS matches: {n_css if n_css is not None else 'n/a (shadow/frame)'}   "
                                f"XPath matches: {n_xp if n_xp is not None else 'n/a (shadow/frame)'}")
            except Exception:  # noqa: BLE001
                pass
        for title, body in self.plugins.export_sections(node, sel):
            lines += [f"# {title}", body, ""]
        return "\n".join(lines)

    # ================================================================== plugins
    def _build_menus(self):
        mb = self.menuBar()
        self.view_menu = mb.addMenu("&View")
        self.export_menu = mb.addMenu("&Export")
        pm = mb.addMenu("&Plugins")
        pm.addAction("Plugin Manager…", self.show_plugin_manager)
        pm.addAction("Open plugins folder", self._open_plugins_folder)
        pm.addSeparator()
        pm.addAction("Writing plugins (docs)", lambda: QDesktopServices.openUrl(QUrl(PLUGIN_DOCS)))
        self._plugin_shortcuts: List[QAction] = []
        self._rebuild_view_menu()

    def _rebuild_view_menu(self):
        if not hasattr(self, "view_menu"):
            return
        self.view_menu.clear()
        for dock in self.findChildren(QDockWidget):
            self.view_menu.addAction(dock.toggleViewAction())

    def _plugins_changed(self, kind: str):
        if kind == "actions":
            self._rebuild_plugin_actions()
        elif kind in ("exporters", "formats"):
            self._rebuild_export_menu()
            if self.current is not None:
                self.regenerate()
        elif kind == "providers" and self.current is not None:
            self.select_node(self.current, reveal=False, flash=False)
        elif kind == "backends":
            self.load_roots()

    def _rebuild_plugin_actions(self):
        while self.plugin_actions_row.count():
            it = self.plugin_actions_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for a in self._plugin_shortcuts:
            self.removeAction(a)
        self._plugin_shortcuts = []
        actions = self.plugins.registry.of("actions")
        if actions:
            self.plugin_actions_row.addWidget(QLabel("Plugins:"))
        for act in actions:
            b = QPushButton(act["name"])
            if act.get("shortcut"):
                b.setToolTip(act["shortcut"])
                qa = QAction(act["name"], self, shortcut=QKeySequence(act["shortcut"]))
                qa.triggered.connect(lambda _=False, a=act: self._run_plugin_action(a))
                self.addAction(qa)
                self._plugin_shortcuts.append(qa)
            b.clicked.connect(lambda _=False, a=act: self._run_plugin_action(a))
            self.plugin_actions_row.addWidget(b)
        self.plugin_actions_row.addStretch(1)

    def _run_plugin_action(self, action: dict, node: Optional[Node] = None):
        node = node or self.current
        if node is None:
            self.statusBar().showMessage("Select an element first", 3000)
            return
        self.driver.submit(lambda: self.plugins.run_action(action, node),
                           self.ui.wrap(lambda msg: self.statusBar().showMessage(msg or "", 4000)),
                           self._err(action["name"]))

    def _rebuild_export_menu(self):
        self.export_menu.clear()
        items = [("format", f) for f in self.plugins.registry.of("formats")] + \
                [("exporter", e) for e in self.plugins.registry.of("exporters")]
        if not items:
            self.export_menu.addAction("No exporters (enable some in Plugins > Plugin Manager)").setEnabled(False)
        for kind, obj in items:
            self.export_menu.addAction(f"Copy as {obj.name}", lambda k=kind, o=obj: self._export_current(k, o))

    def _export_current(self, kind: str, obj):
        node = self.current
        text = self.selector_text()
        if node is None or not text:
            self.statusBar().showMessage("Select an element first", 3000)
            return

        def job():
            from ..core.selector import parse
            if not obj.applies_to(node):
                return None
            if kind == "format":
                return obj.from_selector(parse(text), node)
            return obj.export(node, text)

        def done(out):
            if out is None:
                self.statusBar().showMessage(f"{obj.name} doesn't apply to this element", 4000)
                return
            QGuiApplication.clipboard().setText(out)
            self.statusBar().showMessage(f"Copied as {obj.name}", 3000)
        self.driver.submit(job, self.ui.wrap(done), self._err(f"Export {obj.name}"))

    def show_plugin_manager(self):
        from .plugin_dialog import PluginManagerDialog
        PluginManagerDialog(self.plugins, self).exec()

    def _open_plugins_folder(self):
        os.makedirs(self.plugins.plugin_dir, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.plugins.plugin_dir))

    # ================================================================== files and help
    def open_selector(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open selector", "", "Selectors (*.sel *.txt);;All files (*)")
        if path:
            with open(path, encoding="utf-8") as f:
                self.sel_text.setPlainText(f.read().strip())
            self.validate()

    def save_selector(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save selector", "selector.sel", "Selectors (*.sel *.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.selector_text() + "\n")

    def syntax_help(self):
        from ..core import selector as selmod
        QMessageBox.information(self, "Selector syntax", selmod.__doc__)
