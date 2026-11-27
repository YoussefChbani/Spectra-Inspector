# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - unreleased

Plugins and extensibility.

### Added

* **Plugins.** A versioned public API, `spectra.plugins` (plugin API 1.0): `PluginInfo`, `register(ctx)` / `unregister(ctx)`, and a `PluginContext` to add exporters, selector formats, actions, dockable panels, property providers, UI technology back ends and event handlers (`element_selected`, `selector_validated`, `pick_started`, `pick_finished`, `app_started`, `app_closing`), with per-plugin settings and thread helpers. Plugins built for another major API version are refused with a clear message.
* **Plugin manager:** plugins are found in Spectra itself, in `%APPDATA%\Spectra\plugins` (files or packages) and in installed packages (`spectra_inspector.plugins` entry points). They can be enabled, disabled and reloaded at run time; a failing plugin is reported in the Log tab and the Plugin Manager and never crashes the app; everything a plugin added is removed when it is disabled.
* **Plugins > Plugin Manager** dialog with a security warning, *Install from file…*, *Reload* and *Open plugins folder*. Spectra asks before it enables a third-party plugin for the first time.
* **View**, **Export** and **Plugins** menus. The Export menu copies the selected element as any exporter or selector format.
* Built-in plugins: Playwright, Selenium and Robot Framework exporters; pywinauto exporter; an XPath-like format for desktop selectors; an *Accessibility summary* in Properties with simple warnings; a *Copy as JSON* action (Ctrl+Shift+J); and a **Python console** panel with `find()`, `find_all()`, `highlight()` and the selected element pre-loaded.
* `examples/plugins/`: a minimal plugin and a pip-installable plugin template. `docs/plugins.md`: guide and API reference.
* **Selector quality, anchors, self-healing and recording.** Four libraries under `spectra/core`: `resilience` grades a selector or an element (resilient, usable, fragile); `anchor` adds `/below/`, `/above/`, `/left/`, `/right/` and `/near/`, and the generator uses one instead of `:nth()` when the rectangles can tell the elements apart; `heal` rebuilds a selector that matches nothing from role, a similar name, and the selected element; `script` turns a series of elements into a Spectra Python script (`Spectra.recording()`). Live validation, Repair, the Locators & code tab and a Target quality property group use them. `grade_selector()` and `Spectra.heal()` expose the same behaviour to scripts.

### Changed

* The Playwright, Selenium and pywinauto snippets of the *Locators & code* tab now come from built-in plugins (they can be switched off).
* Plugin back ends take part in on-screen picking, the UI tree and the selector parser (new technology prefixes).

## [0.1.0] - 2026-11-26

First public version.

### Added

* **Pick on screen (F3):** hover with a live, DPI-correct highlight, then click to capture (the click is not passed to the application) or press Ctrl. Esc or a right-click cancels. Spectra minimizes while you pick (toolbar toggle).
* **UI tree** for the desktop (UI Automation control and raw views), connected browsers (live DOM, shadow roots, iframes) and Java applications.
* **Properties:** every UI Automation property grouped by pattern, MSAA, Win32 details, Java, HTML attributes, accessibility role and name, geometry.
* **Selector language** with `>` / `>>` steps, `=` with `*` and `?` wildcards (`\*` for a literal star), `!=`, `*=`, `^=`, `$=`, `~=` regex, a case-insensitive flag, `:nth()` and `:visible`.
* **Selector generator** that finds the shortest unique selector, adding ancestors, `:nth()` (including on identical top-level windows) or a full path when needed.
* **Selector editor** with ancestry levels, attributes and match operators, live validation, Highlight matches, Regenerate and Repair.
* **Web pages without a browser extension** over the DevTools protocol: launch or connect to Chrome, Edge and other Chromium browsers, F3 hover-picking, F4 browser inspect mode, CSS and XPath with match counts, Playwright and Selenium snippets.
* **Java** applications through the Java Access Bridge.
* **Try it** actions: Focus, Click, Invoke, Toggle, Expand/Collapse, Select, Set value, Type text, Scroll into view, element screenshots.
* **Follow focus**, save and open selector files, F1 syntax help.
* **Python scripting API** (`spectra.api.Spectra`) using the same selectors.
* Crash and error log in `%LOCALAPPDATA%\Spectra\spectra.log`.
* Windows executable built with PyInstaller.

### Performance

* Desktop windows are listed from visible top-level windows instead of a UI Automation search over every hidden window (about 10x faster).
* All properties of an element are read in one cross-process request.
* Browser debugging ports are probed in parallel with a short timeout, so start-up no longer waits about 12 s when no browser is running.
* Tree selection is debounced and stale work is dropped, so arrow-keying through the tree stays responsive.

[0.2.0]: https://github.com/KaramBelmoujoud/Spectra-Inspector/compare/v0.1.0...feature/plugins
[0.1.0]: https://github.com/KaramBelmoujoud/Spectra-Inspector/releases/tag/v0.1.0
