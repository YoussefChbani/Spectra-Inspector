# Writing plugins for Spectra Inspector

Plugins add features to Spectra the way add-ons do in Blender: new code exporters, selector formats, actions, panels, property checks, or even whole new UI technologies. A plugin is ordinary Python. It imports **only** `spectra.plugins`, the versioned public API described here.

> **Security:** plugins run arbitrary Python code with your permissions. They can read your files, control your applications and use the network. Only install and enable plugins you trust. Spectra asks before it enables a third-party plugin for the first time.

- [Quick start (5 minutes)](#quick-start-5-minutes)
- [Where plugins come from](#where-plugins-come-from)
- [Lifecycle](#lifecycle)
- [Threading rules](#threading-rules)
- [API reference](#api-reference)
- [Events](#events)
- [Versioning and compatibility](#versioning-and-compatibility)
- [Packaging and publishing](#packaging-and-publishing)
- [Debugging tips](#debugging-tips)
- [Built-in plugins](#built-in-plugins)

## Quick start (5 minutes)

1. Create `%APPDATA%\Spectra\plugins\hello.py`:

   ```python
   from spectra.plugins import PluginInfo

   PLUGIN = PluginInfo(id="hello", name="Hello", version="1.0", author="You",
                       description="My first Spectra plugin")

   def say_hello(node, ctx):
       return f"Hello {node.label()}"      # shown in the status bar

   def register(ctx):
       ctx.add_action("Say hello", say_hello)
   ```

2. In Spectra, open **Plugins > Plugin Manager**, press **Rescan**, tick **Hello** and confirm.
3. Pick an element (F3). A **Say hello** button appears in the **Try it** tab and in the tree's right-click menu under *Plugin actions*.
4. Edit the file and press **Reload** in the Plugin Manager to load your changes; there's no need to restart Spectra.

A longer example with a panel is in [`examples/plugins/hello_spectra.py`](../examples/plugins/hello_spectra.py), and a pip-installable template is in [`examples/plugins/spectra-plugin-template/`](../examples/plugins/spectra-plugin-template/).

## Where plugins come from

| Source | Where | Enabled by default |
|---|---|---|
| Built-in | `spectra/builtin_plugins/` (shipped with Spectra) | Yes |
| Plugins folder | `%APPDATA%\Spectra\plugins\`: a single `name.py` file, or a package folder `name\__init__.py` | No: you enable it, after a confirmation |
| Installed package | any installed distribution with an entry point in the `spectra_inspector.plugins` group | No: you enable it, after a confirmation |

**Plugins > Plugin Manager** lists every plugin with its version, author, source and status, and lets you enable, disable, reload and install them (**Install from file…** copies a `.py` file or a `.zip` into the plugins folder). The enabled/disabled state is remembered in `%APPDATA%\Spectra\plugins.json`. If two sources define the same plugin id, the first one (in the order of the table) wins.

## Lifecycle

1. **Discovery.** At start-up (and on *Rescan*) Spectra imports each plugin module to read its `PLUGIN` definition. Keep module-level code cheap: no window creation, no network, no long work at import time.
2. **Checks.** The module must define `PLUGIN = PluginInfo(...)` and a `register(ctx)` function, and the major number of `PLUGIN.api_version` must equal Spectra's (see [Versioning](#versioning-and-compatibility)). Otherwise the plugin is listed with an error and isn't run.
3. **Enable.** `register(ctx)` is called on the UI thread with a fresh `PluginContext`. Add your features through `ctx`. If `register` raises, everything it had already added is removed and the error is shown.
4. **Disable / reload / quit.** `unregister(ctx)` is called if you define it; then everything you added through `ctx` is removed automatically (exporters, actions, panels, event subscriptions, back ends...). *Reload* disables the plugin, re-imports its code and enables it again.

Errors in any plugin code are caught, written to the **Log** tab and shown in the Plugin Manager. They never crash Spectra.

## Threading rules

Spectra runs every UI Automation, DevTools and Java Access Bridge call on one background **driver thread**. Element objects (`Node`) and back ends may only be used there; the Qt UI lives on the **UI thread**.

| Your code | Runs on | May use elements directly? | May create Qt widgets? |
|---|---|---|---|
| `register` / `unregister` | UI thread | No | Yes |
| Exporter, selector format, property provider | driver thread | Yes | No |
| Action callback | driver thread | Yes | No (use `ctx.run_in_ui`) |
| Panel factory, event callbacks | UI thread | No (use `ctx.run_in_driver`) | Yes |

- From the UI thread, get element data with a callback: `ctx.run_in_driver(lambda: node.label(), lambda text: label.setText(text))`.
- From the driver thread, update the UI with `ctx.run_in_ui(fn)`.
- Never block the UI thread waiting for the driver (`ctx.run_in_driver(fn)` without a callback): UI Automation sometimes needs the UI thread to answer for Spectra's own windows, and the app would freeze.
- `ctx.log`, `ctx.selector_text`, `ctx.set_selector_text`, `ctx.highlight` and `ctx.select` are safe from any thread.

## API reference

Everything below is importable from `spectra.plugins`.

### `API_VERSION`

The plugin API version of this Spectra, currently `"1.0"`.

### `PluginInfo`

```python
PluginInfo(id: str, name: str, version: str = "0.1", author: str = "", description: str = "",
           api_version: str = "1.0", homepage: str = "")
```

Put one in a module-level `PLUGIN` variable. `id` must be unique and stable (letters, digits, `-`, `_`, `.`): it names the settings file and the saved enabled state. `api_version` is the API version you wrote the plugin for.

### `register(ctx)` and `unregister(ctx)`

`register(ctx: PluginContext)` is required; `unregister(ctx)` is optional. See [Lifecycle](#lifecycle).

### `PluginContext`

Every `add_*` method and `on` return a `Handle`; `handle.remove()` undoes that single registration. You don't need to keep handles: everything is removed when the plugin is disabled.

| Method / property | Description |
|---|---|
| `ctx.add_exporter(exporter)` | Add an `Exporter` (class or instance). Its output becomes a section of the **Locators & code** tab and an entry of the **Export** menu. |
| `ctx.add_selector_format(fmt)` | Add a `SelectorFormat` (class or instance), shown like an exporter. |
| `ctx.add_action(name, callback, applies_to=None, shortcut=None)` | Add a button to the **Try it** tab and the tree's *Plugin actions* menu. `callback(node, ctx)` runs on the driver thread; a returned string goes to the status bar. `applies_to(node) -> bool` (driver thread) can refuse an element. `shortcut` is a Qt key sequence such as `"Ctrl+Shift+J"`. |
| `ctx.add_panel(title, widget_factory)` | Add a dockable panel. `widget_factory(ctx) -> QWidget` runs on the UI thread. The panel is listed in the **View** menu. |
| `ctx.add_property_provider(provider)` | `provider(node) -> [(group, name, value), ...]` adds rows to the **Properties** panel (driver thread). |
| `ctx.add_backend(backend)` | Add a whole UI technology: a `Backend` subclass or instance. Its `tech` (lowercase letters, digits, `_`) becomes a selector prefix, its `root()` joins the UI tree, and its `from_point(x, y)` is tried when picking. |
| `ctx.on(event, callback)` | Subscribe to an [event](#events). |
| `ctx.engine` | The selector engine: `engine.find_all(selector, timeout=10)` returns a result with `.matches`, `.error`, `.elapsed_ms`; `engine.generate(node)` returns `(Selector, result)`. Driver thread only. |
| `ctx.run_in_driver(fn, callback=None, timeout=30)` | Run `fn` on the driver thread. With `callback`, return at once and call `callback(result)` on the UI thread; without it, wait and return the result (not from the UI thread). |
| `ctx.run_in_ui(fn, wait=False, timeout=30)` | Run `fn` on the UI thread; with `wait=True`, block and return its result. |
| `ctx.log(msg)` | Write `[plugin-id] msg` to the **Log** tab. Any thread. |
| `ctx.settings` | A dict saved as JSON in `%APPDATA%\Spectra\plugins\<id>.json` whenever you assign a key. After mutating a nested value in place, call `ctx.settings.save()`. Values must be JSON-serialisable. |
| `ctx.current_node()` | The selected element (`Node`) or `None`. |
| `ctx.selector_text()` / `ctx.set_selector_text(text, validate=True)` | Read or replace the selector editor's text. Any thread. |
| `ctx.highlight(node)` | Frame an element on screen. Any thread. |
| `ctx.select(node)` | Make `node` the selected element, as if it had been picked. Any thread. |
| `ctx.plugin_id`, `ctx.info` | Your plugin's id and `PluginInfo`. |

### `Exporter`

```python
class MyExporter(Exporter):
    name = "My tool"                                   # section title / Export menu entry

    def applies_to(self, node) -> bool:                # optional, default True
        return node.tech == "web"

    def export(self, node, selector_text: str) -> str: # required
        return f'my_tool.click("{selector_text}")'
```

Both methods run on the driver thread. `selector_text` is the Spectra selector of the element.

### `SelectorFormat`

```python
class MyFormat(SelectorFormat):
    name = "My notation"
    def applies_to(self, node) -> bool: ...                  # optional
    def from_selector(self, selector: Selector, node) -> str: ...  # required
    def to_selector(self, text: str) -> Optional[Selector]: ...    # optional, return None if not supported
```

`selector.steps` is a list of `Step(role, filters, nth, visible, deep, tech, relation)`; `relation` is `below`, `above`, `left`, `right`, `near`, or None. Each `Filter(name, op, value, case_insensitive)` has `has_wildcards` and `literal` helpers. `spectra.plugins.parse(text)` parses Spectra's own syntax.

### `Node` and `Backend`

The element model shared by every technology (`spectra.core.node`). A `Node` has `tech`, `key()`, `role()`, `get(attr)`, `selector_attrs()`, `suggested_attrs(is_top)`, `properties()`, `label()`, `rect()`, `children()`, `parent()`, `ancestry()`, and optionally `find(step, deep, budget)` as a fast search path. A `Backend` has `tech`, `title`, `root()`, `from_point(x, y)` and `available()`. See CONTRIBUTING.md, *Adding a back end*, for details. Technology-specific nodes add more (for example `node.page` and `node.bid` on web nodes); those are not part of the stable API.

## Events

Subscribe with `ctx.on(event, callback)`. Callbacks run on the UI thread. An exception in one callback is logged and doesn't stop the others.

| Event | Callback | When |
|---|---|---|
| `app_started` | `callback()` | Spectra finished starting (back ends are ready). Plugins enabled later don't get it. |
| `app_closing` | `callback()` | The main window is closing. |
| `element_selected` | `callback(node)` | An element was selected: picked, clicked in the tree, followed focus, repaired... |
| `selector_validated` | `callback(selector_text, result)` | A selector was generated or validated. `result.matches`, `result.error`, `result.elapsed_ms`. |
| `pick_started` | `callback()` | On-screen picking (F3) started. |
| `pick_finished` | `callback(node_or_None)` | Picking ended with a captured element, or `None` if it was cancelled. |

## Versioning and compatibility

- The plugin API has its own version, `spectra.plugins.API_VERSION` (now `1.0`), separate from the app version.
- Within a major version (`1.x`), changes are **additive only**: new methods, new events, new optional arguments. A plugin written for `1.0` keeps working with every `1.x`.
- Removing or changing anything is a **major** change (`2.0`). Spectra refuses to load a plugin whose `api_version` has a different major number, and says so in the Plugin Manager, instead of letting it fail in odd ways.
- Only `spectra.plugins` is covered. Anything else in Spectra (`spectra.gui`, `spectra.backends`, private attributes of nodes) can change in any release.
- If you use something added in a later minor version, say so in your description (for example "needs Spectra 0.3 / plugin API 1.1").

## Packaging and publishing

**Single file:** share `my_plugin.py`. Users drop it in `%APPDATA%\Spectra\plugins\` or use **Install from file…**.

**Package folder or zip:** a folder with `__init__.py` (and any other modules; relative imports work). Zip the folder for **Install from file…**.

**Python package on PyPI:** declare an entry point so Spectra finds the plugin once it's installed:

```toml
[project]
name = "spectra-plugin-mytool"
dependencies = ["spectra-inspector>=0.2,<1"]

[project.entry-points."spectra_inspector.plugins"]
mytool = "spectra_plugin_mytool"        # plugin id = module that defines PLUGIN and register()
```

Build and upload with `python -m build` and `python -m twine upload dist/*`. Users install it with `pip install spectra-plugin-mytool` into the same environment as Spectra, then enable it in the Plugin Manager. [`examples/plugins/spectra-plugin-template/`](../examples/plugins/spectra-plugin-template/) is a complete starting point.

The standalone `SpectraInspector.exe` doesn't include pip. It loads plugins from the plugins folder, and those plugins can only import what is bundled in the executable: Spectra itself, its dependencies (PySide6, comtypes, websocket-client, Pillow) and the parts of the standard library they use. Plugins with other dependencies need Spectra installed from source or with pip.

## Debugging tips

- Open **View > Python console** to try code with the same objects a plugin gets: `node`, `find()`, `find_all()`, `highlight()`, `engine` and `ctx` (the console's own context).
- Errors from your plugin appear in the **Log** tab with a traceback, and in the Plugin Manager details pane.
- `ctx.log(...)` is the simplest print-debugging that works from every thread.
- After editing your file, press **Reload** in the Plugin Manager.
- Start Spectra from a terminal (`python -m spectra`) to see anything your plugin prints.
- Unit-test the parts that don't need the GUI: `spectra.plugin_manager.PluginManager` works with a fake host (see `tests/test_plugins.py`).

## Built-in plugins

These ship with Spectra, are on by default, and are written against the same public API, so they double as examples (`spectra/builtin_plugins/`).

| Plugin | Adds |
|---|---|
| Web code exporters | Playwright, Selenium and Robot Framework (Browser library) code for web elements, and a Robot Framework keyword that uses the Spectra API for desktop elements |
| pywinauto exporter | pywinauto (UIA back end) code for desktop elements |
| XPath-like format (UIA) | `/Window[@Name='…']//Edit[@AutomationId='…']` notation for desktop selectors (output only) |
| Accessibility summary | An *Accessibility summary* group in Properties: accessible name, role, keyboard focus, and warnings such as a button without a name |
| Target quality | A *Target quality* group in Properties: how stable a handle this element has (test id, automation id, accessible name) and why |
| Copy element as JSON | A *Copy as JSON* action (Ctrl+Shift+J) |
| Python console | The **Python console** panel |
