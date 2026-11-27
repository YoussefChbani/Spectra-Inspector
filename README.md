# Spectra Inspector

[![CI](https://github.com/KaramBelmoujoud/Spectra-Inspector/actions/workflows/ci.yml/badge.svg)](https://github.com/KaramBelmoujoud/Spectra-Inspector/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)
![Platform](https://img.shields.io/badge/platform-Windows-0078d6)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

**Point at any element of a Windows app or a web page, and get a readable selector that finds it again.**

Spectra Inspector is a UI element inspector for Windows desktop, Java and web apps. Pick an element on screen, browse the UI tree, read every property, and generate, edit and validate selectors. Web pages are read directly over the browser's DevTools protocol, so there is **no browser extension to install**, and shadow DOM and iframes are handled. The same selectors work from Python through a small scripting API.

## Screenshots

| Desktop app (UI Automation) | Web page (DevTools) |
|---|---|
| ![A Character Map text box selected: its ancestry, attributes, selector and UI Automation properties](docs/images/desktop-element.png) | ![The third "Add to cart" button of a demo store: selector with :nth(3), CSS, XPath and code](docs/images/web-element.png) |

![A button inside a shadow DOM web component, found with a plain >> selector](docs/images/shadow-dom.png)

## Features

| Area | What Spectra does |
|---|---|
| Pick on screen | F3, hover, then click (the click is not passed to the app) or press Ctrl. Live highlight that is DPI-correct on mixed-scale monitors. Spectra minimizes while you pick. |
| UI tree | Lazy and filterable. Desktop (UI Automation control or raw view), browsers (DOM) and Java apps side by side |
| Properties | Every UIA property grouped by pattern; MSAA (LegacyIAccessible); Win32 (hwnd, class, styles, control id, process path); Java; HTML attributes, accessibility role and name, geometry |
| Selectors | A readable language with wildcards, regex, contains/starts/ends, a case-insensitive flag, `:nth()`, `:visible`, and anchors (`/below/`, `/above/`, `/left/`, `/right/`, `/near/`) |
| Selector editor | Tick ancestry levels and attributes, change the match operator, edit values; live validation shows the match count, time and which step failed |
| Repair | Drops one attribute at a time, then self-heals by role and a similar name when that is not enough |
| Web without an extension | DevTools protocol: live DOM, **shadow DOM and iframes crossed by `>>`**, in-page highlight, the browser's own click-to-inspect (F4), hover-pick (F3) |
| Web outputs | Spectra selector, CSS and XPath (each checked against the page), Playwright and Selenium snippets |
| Web without a debug port | UI Automation reads the browser's accessibility tree (roles, names, values) |
| Electron / WebView2 | Same DevTools path when started with a debugging port |
| Java | Java Access Bridge tree, properties and hover-pick |
| Try it | Focus, Click, Invoke, Toggle, Expand/Collapse, Select, Set value, Type text, Scroll into view |
| Screenshots | Screenshot of one element (the full element for web pages, even when it is off-screen) |
| Code | Spectra Python API, pywinauto, Playwright and Selenium snippets |
| Follow focus | Shows whichever element has keyboard focus as you tab around |

## Install

Spectra runs on Windows 10 and 11 with a 64-bit Python 3.10 or newer.

**From source (simplest):** clone the repository and double-click **`run.bat`**. The first run creates `.venv` and installs the dependencies (PySide6, comtypes, websocket-client, Pillow), which takes about a minute.

**With pip:**

```powershell
git clone https://github.com/KaramBelmoujoud/Spectra-Inspector.git
cd Spectra-Inspector
python -m pip install .
spectra-inspector
```

**Standalone .exe:** download the zip from the [Releases](https://github.com/KaramBelmoujoud/Spectra-Inspector/releases) page, unzip it anywhere and run `SpectraInspector.exe`. No Python needed. To build it yourself, see [Building the exe](#building-the-exe).

## Quick start

1. Start Spectra (`run.bat`, `spectra-inspector`, or `python -m spectra`).
2. Press **F3**, hover any element, and **click** it (or press **Ctrl**) to capture it. **Esc** or a right-click cancels.
3. Spectra shows the element's ancestry, attributes, properties and a generated selector. Tick or untick attributes, change how they match, and watch the live validation.
4. Use **Highlight matches** to see every element the selector finds, and the **Locators & code** tab for ready-to-paste code.

## Selector language

```
uia:Window[process="notepad.exe"][name="* - Notepad"] >> Document
web:tab[host="github.com"] >> button[text="Sign in" i]
web:tab[title*="Inbox"] >> iframe >> input[aria-label="Search"]
java:frame[name="SwingSet2"] >> push-button[name="OK"]:nth(2)
uia:Window[process="notepad.exe"] >> Text[name="Email"] /below/ Edit
```

* A step has the form `role[attr op "value"]…:pseudo`. The first step may carry a technology prefix: `uia:` (default), `web:` or `java:`.
* `>` means direct child. `>>` (or a space) means any descendant, and crosses shadow roots and iframes on web pages.
* `/below/`, `/above/`, `/left/` and `/right/` mean the nearest element in that direction from the previous match, searched in the same window or tab. `/near/` is the nearest element within 96 pixels when it does not line up on an axis. These are not parent/child links: the target is usually a sibling of the landmark. Without `:nth()`, only the nearest match is used. `:nth(2)` is the second nearest. Elements need an on-screen rectangle; two elements on the same rectangle still use `:nth()`.
* Operators: `=` equals, with `*` (any text) and `?` (one character) as wildcards, for example `[name="* - File Explorer"]`. Write `\*` or `\?` for a real star or question mark (generated selectors do this for you). `!=` is the opposite of `=`, `*=` contains, `^=` starts with, `$=` ends with, `~=` regex, and `?=` is an alias of `=`. Add ` i` before `]` to ignore case. `[attr]` means the attribute exists and isn't empty.
* `:nth(N)` takes the N-th match (1-based; `-1` is the last). `:visible` keeps only elements with a non-empty on-screen rectangle.
* UIA attributes: `name`, `automationid`, `class`, `process`, `framework`, `type`, `controlid`, `hwnd`, `helptext`, `ariarole`, `value`, `pid`.
* Web attributes: any HTML attribute, plus `text` (visible text), `own-text` and `tag`. Tab attributes: `title`, `url`, `host`.
* Java attributes: `name`, `description`, `virtualname`, `states`, `value`, `index`, `process`.

Press **F1** in the app for the full syntax. The guide to scores, anchors, repair, and recording is [docs/README.md](docs/README.md).

## Browser inspection

Chromium browsers (Chrome, Edge, Brave, Opera, Vivaldi), Electron apps and WebView2 hosts include the DevTools protocol. Spectra connects to it directly, so there's nothing to install in the browser.

* **Browsers ▸ Launch Chrome/Edge with inspection enabled** starts the browser on a free port with its own profile folder. Recent Chrome versions refuse remote debugging on your default profile, so it has to be a separate profile. Anything you sign in to there is kept for next time.
* Or start it yourself with `chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\spectra-profile` and use **Browsers ▸ Connect** (Spectra also scans common ports at start-up).
* WebView2 apps: set `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9229` before starting the app. Electron apps: start them with `--remote-debugging-port=9229`.
* **F4** uses the browser's own inspect mode: click an element in the page and it lands in Spectra. **F3** hover-picking works inside connected browsers too, and you can switch that off in the Browsers menu.
* Even with no debugging port, F3 still returns page elements through UI Automation, because browsers expose an accessibility tree.

## Java

Run `jabswitch -enable` once from any JDK/JRE `bin` folder, then restart the Java application. Spectra finds `WindowsAccessBridge-64.dll` through `JAVA_HOME`, `PATH`, System32 or the usual JDK install folders. Use a 64-bit Python.

## Scripting API

```python
from spectra.api import Spectra

s = Spectra(browser_port=9222)          # leave out browser_port for desktop-only use
s.find('uia:Window[process="notepad.exe"] >> Document').type_text("Hello")
s.find('web:tab[host="example.com"] >> a').click()
fixed = s.heal('uia:Window[process="notepad.exe"] >> Button[name="Sav"]')  # name changed

for button in s.find_all('web:tab[title="Shop"] >> button[class="add"]'):
    print(button.name, button.rect)

rec = s.recording()
rec.click(fixed.element)
print(rec.to_python())
```

`grade_selector("button:nth(2)")` scores a selector as resilient, usable or fragile without starting the app. The same score is shown next to live validation and in the Locators & code tab. How to use the score, anchors, repair, and recording is in [docs/README.md](docs/README.md).

`find` waits up to `timeout` seconds; elements offer `click`, `type_text`, `set_value`, `invoke`, `focus`, `get(attr)`, `rect` and `properties()`.

## Plugins

Spectra can be extended with plugins, much like add-ons in Blender: new code exporters, selector formats, actions, panels, property checks, or whole new UI technologies. A plugin is a Python file that imports the versioned `spectra.plugins` API:

```python
from spectra.plugins import PluginInfo

PLUGIN = PluginInfo(id="hello", name="Hello", version="1.0", author="You")

def register(ctx):
    ctx.add_action("Say hello", lambda node, ctx: f"Hello {node.label()}")
```

Drop it in `%APPDATA%\Spectra\plugins\`, or `pip install` a package that declares a `spectra_inspector.plugins` entry point, and enable it in **Plugins > Plugin Manager**. The Playwright, Selenium, Robot Framework and pywinauto exporters, an XPath-like format, an accessibility summary, *Copy as JSON* and a **Python console** panel are built-in plugins written the same way.

> Plugins run arbitrary Python code with your permissions. Only install plugins you trust.

Read [docs/plugins.md](docs/plugins.md) for the guide and API reference, and [examples/plugins/](examples/plugins/) for a minimal plugin and a pip-installable template.

![The Plugin Manager](docs/images/plugin-manager.png)

## Building the exe

```powershell
build_exe.bat
```

This installs the dev dependencies into `.venv`, runs PyInstaller with `spectra.spec` and produces `dist\SpectraInspector\SpectraInspector.exe`. The folder also gets `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` and `licenses\`; zip the whole `dist\SpectraInspector` folder to share it (keep it a one-folder build, see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)). Tagged releases (`v*`) are built the same way by GitHub Actions.

## Running tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

or `run_tests.bat`. The tests cover the selector language and generator, a live Chromium page (skipped when Chrome or Edge isn't installed) and live UI Automation checks against Character Map (Windows only; they open and close their own Character Map window). `ruff check .` runs the linter.

## Project layout

```
spectra/core/selector.py   selector language: parser, serializer, matching
spectra/core/engine.py     search (step by step, with budgets and timeouts) and selector generation
spectra/core/resilience.py grades a selector or an element: resilient, usable or fragile
spectra/core/anchor.py     relative selectors (/below/ and the others) and the generator's anchor choice
spectra/core/heal.py       rebuilds a selector when the old one matches nothing
spectra/core/script.py     turns a series of elements into a Spectra Python script
spectra/core/webpaths.py   CSS / XPath generation
spectra/backends/uia.py    UI Automation (comtypes, cached FindAll, native conditions)
spectra/backends/win32.py  Win32 helpers (windows, processes, DPI, Chromium viewports, mouse hook)
spectra/backends/web.py    DevTools-based DOM model (tabs, shadow roots, iframes)
spectra/backends/cdp.py    DevTools protocol client and browser launcher
spectra/backends/jab.py    Java Access Bridge
spectra/service.py         point-to-element routing across technologies
spectra/driver.py          single background thread for all automation calls
spectra/gui/               PySide6 interface and highlight overlay
spectra/api.py             scripting API
spectra/plugins/           the public plugin API (spectra.plugins)
spectra/plugin_manager.py  plugin discovery, loading, isolation and cleanup
spectra/builtin_plugins/   plugins that ship with Spectra (exporters, console, ...)
tests/                     unit tests, a live Chromium test and live Windows tests
demo/                      a demo web page and the script that records the demo video
docs/                      guides (plugins.md, stable selectors in README.md) and screenshots
examples/plugins/          a minimal plugin and a pip-installable plugin template
```

## Roadmap

* Cross-site (out-of-process) iframes in the DOM tree
* Firefox DOM access (today Firefox is inspected through UI Automation)
* SAP GUI scripting back end
* An event recorder that turns clicks and typing into a script
* More built-in plugins (Gherkin steps, test-data capture) and a plugin index

## Known limits

* Cross-site iframes that run in a separate process aren't in the DOM tree yet. Same-site iframes and shadow DOM are.
* Searching `>>` from the desktop root across every application can be slow. Start selectors with a window step (the generator always does).
* `/below/` and the other anchors need each element to report a rectangle. When two elements share a rectangle, the generator keeps `:nth()`.
* Java coordinates on high-DPI screens depend on the Java version's DPI awareness.
* Not supported yet: SAP GUI scripting, Silverlight, the legacy IE engine and Citrix/RDP virtual channels.

## Contributing

Bug reports, ideas and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). For security issues, see [SECURITY.md](SECURITY.md).

## License

Spectra Inspector is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE).

It uses Qt and PySide6 under the LGPL-3.0 and other open-source components; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The Windows executable is a one-folder build, so the Qt/PySide6 libraries inside it can be replaced, and it ships with these notices and the LGPL/GPL texts.
