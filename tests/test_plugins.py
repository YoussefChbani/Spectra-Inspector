"""The plugin system: discovery, isolation, cleanup, settings, events, built-in plugins and the console.
No GUI and no real applications are needed."""
import json
import os
import textwrap
import types

import pytest

from spectra.core.engine import Engine
from spectra.core.node import Backend, Node
from spectra.core.selector import parse
from spectra.plugin_manager import Host, PluginManager
from spectra.plugins import API_VERSION, PluginInfo


# ---------------------------------------------------------------------- fakes
class FakeUia(Node):
    tech = "uia"

    def __init__(self, role, parent=None, **attrs):
        self._role, self._parent, self._attrs, self._kids = role, parent, attrs, []
        if parent:
            parent._kids.append(self)

    def key(self):
        return id(self)

    def role(self):
        return self._role

    def selector_attrs(self):
        return self._attrs

    def suggested_attrs(self, is_top=False):
        return ["name"]

    def children(self):
        return self._kids

    def parent(self):
        return self._parent

    def rect(self):
        return (0, 0, 10, 10)

    def prop(self, pid):
        return {30009: True, 30010: True}.get(pid)


class FakeUiaBackend(Backend):
    tech = "uia"

    def __init__(self, root):
        self._root = root

    def root(self):
        return self._root


class FakeWeb(Node):
    """Just enough of a DevTools DOM node for the CSS / XPath generators."""
    tech = "web"

    def __init__(self, tag, attrs, parent=None):
        self._tag, self._a, self._parent, self._kids = tag, attrs, parent, []
        self.raw = {"nodeId": id(self), "parentId": id(parent) if parent else None}
        if parent:
            parent._kids.append(self)

    def key(self):
        return id(self)

    def role(self):
        return self._tag

    def html_attrs(self):
        return self._a

    def selector_attrs(self):
        return dict(self._a, text=self._a.get("text", ""))

    def parent(self):
        return self._parent


class FakeHost(Host):
    def __init__(self, engine=None):
        self.engine = engine or Engine()
        self.logs = []
        self.changes = []
        self.panels = []
        self.allow = False
        self.node = None

    def log(self, msg):
        self.logs.append(msg)

    def plugins_changed(self, kind):
        self.changes.append(kind)

    def add_panel(self, plugin_id, panel):
        self.panels.append((plugin_id, panel["title"]))

    def remove_panel(self, plugin_id, panel):
        self.panels.remove((plugin_id, panel["title"]))

    def confirm_third_party(self, record):
        return self.allow

    def current_node(self):
        return self.node

    def selector_text(self):
        return 'uia:Window[name="Demo"] >> Button[name="OK"]'


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SPECTRA_HOME", str(tmp_path / "home"))
    folder = tmp_path / "home" / "plugins"
    folder.mkdir(parents=True)
    return folder


def write(folder, name, code):
    path = folder / name
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    return path


GOOD = '''
from spectra.plugins import PluginInfo, Exporter, SelectorFormat, Backend, Node

PLUGIN = PluginInfo(id="good", name="Good plugin", version="1.2", author="Tester")
seen = []

class Ex(Exporter):
    name = "Fake exporter"
    def export(self, node, selector_text):
        return "EXPORT " + selector_text

class Fmt(SelectorFormat):
    name = "Fake format"
    def from_selector(self, selector, node):
        return "FMT " + str(len(selector.steps))

class Root(Node):
    tech = "fake"
    def key(self): return "root"
    def role(self): return "Root"

class FakeBackend(Backend):
    tech = "fake"
    def root(self): return Root()

def register(ctx):
    ctx.add_exporter(Ex)
    ctx.add_selector_format(Fmt())
    ctx.add_action("Shout", lambda node, ctx: "shouted " + node.role(), shortcut="Ctrl+Shift+H")
    ctx.add_property_provider(lambda node: [("Good", "role", node.role())])
    ctx.add_panel("Good panel", lambda ctx: None)
    ctx.add_backend(FakeBackend)
    ctx.on("element_selected", lambda node: seen.append(node))
    ctx.on("app_started", lambda: 1 / 0)

def unregister(ctx):
    seen.append("bye")
'''


def make(home, host=None, **kw):
    return PluginManager(host or FakeHost(), plugin_dir=str(home), include_builtin=kw.pop("builtin", False),
                         include_entry_points=kw.pop("eps", False))


# ---------------------------------------------------------------------- discovery and loading
def test_folder_discovery_file_and_package(home):
    write(home, "good.py", GOOD)
    pkg = home / "pkgplugin"
    pkg.mkdir()
    write(pkg, "__init__.py", '''
        from spectra.plugins import PluginInfo
        from .helper import answer
        PLUGIN = PluginInfo(id="pkgplugin", name="Package plugin")
        def register(ctx):
            ctx.log(f"answer is {answer()}")
    ''')
    write(pkg, "helper.py", "def answer():\n    return 42\n")
    m = make(home)
    recs = {r.id: r for r in m.discover()}
    assert set(recs) == {"good", "pkgplugin"}
    assert recs["good"].info.version == "1.2" and recs["good"].source == "folder"
    assert not recs["good"].enabled  # third-party plugins stay off until the user agrees
    assert m.enable("pkgplugin", confirmed=True)
    assert any("answer is 42" in line for line in m.host.logs)


def test_third_party_needs_confirmation_and_is_remembered(home):
    write(home, "good.py", GOOD)
    host = FakeHost()
    m = make(home, host)
    m.discover()
    assert not m.enable("good")             # the user said no
    host.allow = True
    assert m.enable("good")                 # the user said yes
    m2 = make(home, FakeHost())             # next start: trusted and enabled without asking
    m2.start()
    assert m2.records["good"].enabled


def test_api_version_mismatch_is_refused(home):
    write(home, "future.py", '''
        from spectra.plugins import PluginInfo
        PLUGIN = PluginInfo(id="future", name="From the future", api_version="2.0")
        def register(ctx):
            raise AssertionError("must not run")
    ''')
    m = make(home)
    m.discover()
    rec = m.records["future"]
    assert "plugin API 2.0" in rec.error and API_VERSION in rec.error
    assert not m.enable("future", confirmed=True)


def test_broken_plugins_are_isolated(home):
    write(home, "crash_import.py", "raise RuntimeError('boom at import')\n")
    write(home, "no_info.py", "def register(ctx):\n    pass\n")
    write(home, "crash_register.py", '''
        from spectra.plugins import PluginInfo
        PLUGIN = PluginInfo(id="crash_register", name="Crashes")
        def register(ctx):
            ctx.add_action("half", lambda n, c: None)
            raise ValueError("boom in register")
    ''')
    write(home, "good.py", GOOD)
    m = make(home)
    m.discover()                                        # no exception escapes
    assert "boom at import" in m.records["crash_import"].error
    assert "PLUGIN" in m.records["no_info"].error
    assert not m.enable("crash_register", confirmed=True)
    assert "boom in register" in m.records["crash_register"].error
    assert m.registry.of("actions") == []              # the half-done registration was undone
    assert m.enable("good", confirmed=True)            # the others still work


def test_entry_point_plugins(home, monkeypatch):
    mod = types.ModuleType("fake_dist_plugin")
    mod.PLUGIN = PluginInfo(id="from_pip", name="Installed with pip")
    mod.register = lambda ctx: ctx.log("hello from pip")

    class EP:
        name, value, group = "from_pip", "fake_dist_plugin", "spectra_inspector.plugins"
        dist = types.SimpleNamespace(name="spectra-plugin-fake")

        def load(self):
            return mod

    from importlib import metadata
    monkeypatch.setattr(metadata, "entry_points", lambda **kw: [EP()] if kw.get("group") == EP.group else [])
    m = make(home, eps=True)
    m.discover()
    rec = m.records["from_pip"]
    assert rec.source == "package" and "spectra-plugin-fake" in rec.location
    assert m.enable("from_pip", confirmed=True)
    assert "hello from pip" in m.host.logs[-1]


# ---------------------------------------------------------------------- what plugins add, and cleanup
def test_register_and_unregister_clean_up_everything(home):
    write(home, "good.py", GOOD)
    host = FakeHost()
    m = make(home, host)
    m.discover()
    assert m.enable("good", confirmed=True)
    good = m.records["good"].module
    reg = m.registry
    assert [e.name for e in reg.of("exporters")] == ["Fake exporter"]
    assert [f.name for f in reg.of("formats")] == ["Fake format"]
    assert [a["name"] for a in reg.of("actions")] == ["Shout"]
    assert host.panels == [("good", "Good panel")]
    assert "fake" in host.engine.backends
    assert parse("fake:Root").tech == "fake"           # the new prefix is accepted by the parser

    root = FakeUia("Desktop")
    win = FakeUia("Window", root, name="Demo")
    btn = FakeUia("Button", win, name="OK")
    host.engine.register(FakeUiaBackend(root))
    sel, _ = host.engine.generate(btn)
    sections = dict(m.export_sections(btn, sel))
    assert sections["Fake exporter"] == "EXPORT " + sel.to_text()
    assert sections["Fake format"] == "FMT 2"
    assert ("Good", "role", "Button") in m.extra_properties(btn)
    assert m.run_action(reg.of("actions")[0], btn) == "shouted Button"

    m.emit("element_selected", btn)
    assert good.seen == [btn]
    m.emit("app_started")                              # the handler divides by zero
    assert any("ZeroDivisionError" in line for line in host.logs)

    m.disable("good")
    assert good.seen[-1] == "bye"
    for kind in reg.KINDS:
        assert reg.items(kind) == [], kind
    assert host.panels == [] and "fake" not in host.engine.backends
    m.emit("element_selected", btn)
    assert good.seen[-1] == "bye"                      # no longer subscribed


def test_handles_remove_single_registrations(home):
    m = make(home)
    write(home, "one.py", '''
        from spectra.plugins import PluginInfo
        PLUGIN = PluginInfo(id="one", name="One")
        handles = {}
        def register(ctx):
            handles["a"] = ctx.add_action("A", lambda n, c: "a")
            handles["b"] = ctx.add_action("B", lambda n, c: "b")
    ''')
    m.discover()
    m.enable("one", confirmed=True)
    m.records["one"].module.handles["a"].remove()
    assert [a["name"] for a in m.registry.of("actions")] == ["B"]
    with pytest.raises(ValueError):
        m.records["one"].ctx.on("no_such_event", print)


def test_settings_persist(home):
    write(home, "good.py", GOOD)
    m = make(home)
    m.discover()
    m.enable("good", confirmed=True)
    ctx = m.records["good"].ctx
    ctx.settings["count"] = 3
    ctx.settings.setdefault("names", []).append("x")
    ctx.settings.save()
    data = json.loads((home / "good.json").read_text())
    assert data == {"count": 3, "names": ["x"]}
    m2 = make(home)
    m2.discover()
    m2.enable("good", confirmed=True)
    assert m2.records["good"].ctx.settings["count"] == 3


def test_enabled_state_is_saved(home):
    write(home, "good.py", GOOD)
    m = make(home)
    m.discover()
    m.enable("good", confirmed=True)
    m.disable("good")
    state = json.loads((home.parent / "plugins.json").read_text())
    assert state["enabled"]["good"] is False and "good" in state["trusted"]
    m2 = make(home)
    m2.start()
    assert not m2.records["good"].enabled


def test_install_from_file(home, tmp_path):
    src = write(tmp_path, "dropped.py", '''
        from spectra.plugins import PluginInfo
        PLUGIN = PluginInfo(id="dropped", name="Dropped in")
        def register(ctx):
            pass
    ''')
    m = make(home)
    assert m.install_file(str(src)) == ["dropped"]
    assert os.path.exists(home / "dropped.py")
    assert not m.records["dropped"].enabled


# ---------------------------------------------------------------------- built-in plugins
@pytest.fixture
def builtins(home):
    host = FakeHost()
    m = PluginManager(host, plugin_dir=str(home), include_builtin=True, include_entry_points=False)
    m.start()
    return m


def test_builtins_load_and_are_on(builtins):
    recs = builtins.records
    for pid in ("web_exporters", "pywinauto_exporter", "xpath_format", "accessibility_summary",
                "copy_json_action", "python_console", "target_quality"):
        assert pid in recs, pid
        assert recs[pid].enabled, (pid, recs[pid].error)
    assert ("python_console", "Python console") in builtins.host.panels


def web_button():
    html = FakeWeb("html", {})
    body = FakeWeb("body", {}, html)
    main = FakeWeb("main", {"id": "shop"}, body)
    return FakeWeb("button", {"class": "add", "text": "Add to cart"}, main)


def test_web_exporters(builtins):
    btn = web_button()
    sel = parse('web:tab[title="Shop"] >> button[text="Add to cart"]')
    sections = dict(builtins.export_sections(btn, sel))
    assert sections["Playwright (Python)"] == 'page.locator("css=main#shop > button").click()'
    assert 'driver.find_element(By.CSS_SELECTOR, "main#shop > button").click()' in sections["Selenium (Python)"]
    assert "Library    Browser" in sections["Robot Framework"]
    assert "Click    css=main#shop > button" in sections["Robot Framework"]
    assert "pywinauto (UIA backend)" not in sections and "XPath-like (UIA)" not in sections


def test_desktop_exporters_and_xpath_format(builtins):
    root = FakeUia("Desktop")
    win = FakeUia("Window", root, name="Character Map", **{"class": "#32770"})
    edit = FakeUia("Edit", win, name="Characters to copy :", automationid="104")
    sel = parse('uia:Window[name="Character*"] >> Edit[automationid="104"]:nth(1)')
    sections = dict(builtins.export_sections(edit, sel))
    assert sections["XPath-like (UIA)"] == "/Window[matches(@Name, '^Character.*$')]//Edit[@AutomationId='104'][1]"
    pw = sections["pywinauto (UIA backend)"]
    assert "Desktop(backend='uia').window(title='Character Map', class_name='#32770')" in pw
    assert "child_window(auto_id='104', title='Characters to copy :', control_type='Edit')" in pw
    rf = sections["Robot Framework"]
    assert "Evaluate" in rf and sel.to_text() in rf and "Call Method    ${element}    click" in rf
    assert "Playwright (Python)" not in sections


def test_accessibility_summary(builtins):
    root = FakeUia("Desktop")
    win = FakeUia("Window", root, name="App")
    nameless = FakeUia("Button", win)
    rows = builtins.extra_properties(nameless)
    assert ("Accessibility summary", "warning", "Button has no accessible name (Name is empty)") in rows
    ok = FakeUia("Button", win, name="OK")
    assert ("Accessibility summary", "warnings", "none") in builtins.extra_properties(ok)
    web_rows = builtins.extra_properties(FakeWeb("button", {}))
    assert any(r[1] == "warning" and "<button> has no accessible name" in r[2] for r in web_rows)


def test_copy_json_action(builtins):
    from spectra.builtin_plugins.copy_json_action import element_json
    root = FakeUia("Desktop")
    btn = FakeUia("Button", FakeUia("Window", root, name="App"), name="OK")
    data = json.loads(element_json(btn, 'uia:Window >> Button[name="OK"]'))
    assert data["tech"] == "uia" and data["role"] == "Button" and data["attributes"] == {"name": "OK"}
    assert data["rect"] == [0, 0, 10, 10] and data["selector"].endswith('Button[name="OK"]')


# ---------------------------------------------------------------------- the console
def test_console_runs_code_and_captures_output(builtins):
    from spectra.builtin_plugins.python_console import ConsoleSession
    root = FakeUia("Desktop")
    win = FakeUia("Window", root, name="Demo")
    FakeUia("Button", win, name="OK")
    builtins.host.engine.register(FakeUiaBackend(root))
    builtins.host.node = win
    s = ConsoleSession(builtins.records["python_console"].ctx)
    assert s.run("1 + 1") == "2\n"
    assert s.run("print('hello')\nx = 5") == "hello\n"
    assert s.run("x * 2") == "10\n"                                      # the namespace persists
    assert s.run("node.role()") == "'Window'\n"                          # the selected element
    assert s.run("find('uia:Window >> Button').get('name')") == "'OK'\n"
    assert s.run("len(find_all('uia:Window'))") == "1\n"
    assert s.run("spectra.API_VERSION") == repr(API_VERSION) + "\n"
    err = s.run("1/0")
    assert "ZeroDivisionError" in err and "<console>" in err and "python_console.py" not in err
    assert "SyntaxError" in s.run("def (")
