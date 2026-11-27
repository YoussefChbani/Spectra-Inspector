# Stable selectors

Spectra points at an element and writes a selector that finds it again. Four packages make that selector last:

| Package | File | What it does |
|---|---|---|
| Selector quality | `spectra/core/resilience.py` | Scores a selector or an element from 0 to 100 |
| Anchors | `spectra/core/anchor.py` | Finds an element by where it sits next to a landmark |
| Self-healing | `spectra/core/heal.py` | Rebuilds a selector that no longer matches |
| Recording | `spectra/core/script.py` | Turns clicks and typing into a Python script |

You do not import these to use Spectra day to day. The app and `spectra.api` call them for you. Import them when you want the score, a repair, or a script from your own code.

## The gap each one closes

**Selector quality.** The generator used to hand you a selector and stop. A numbered id such as `ok1`, or `:nth(2)`, looks fine until the screen changes. The score tells you that before you paste the selector into a test.

**Anchors.** Two buttons named OK used to become `:nth(2)`. Insert one button and the index points at the wrong one. An anchor names a neighbor that stays put, such as the Email label, and takes the button next to it.

**Self-healing.** Repair used to drop one attribute and then give up. A small rename, such as `Save changse` instead of `Save changes`, meant picking the element again. Healing finds the closest element and writes a new selector.

**Recording.** There was no way to turn a series of elements into a script. Recording stores the selector Spectra would write for each step, and joins consecutive typing into one call.

## In the app

1. Pick an element (F3 on Windows, or a connected browser).
2. Read the status line under the selector. A unique match also shows a band and a score, for example `resilient 98`. Hover that line for the reasons.
3. Open **Locators & code**. The first comments are the same score and the reasons.
4. Open **Properties** and find **Target quality**. That score is about the element itself: does it have a test id, an automation id, or a visible name worth using?
5. If the generator anchored the element, the status bar says so. The selector contains `/below/`, `/above/`, `/left/`, `/right/`, or `/near/`.
6. When a saved selector stops matching, paste it into the selector box and press **Repair**. Spectra tries dropping an attribute first. If that fails, it self-heals. The status bar shows the confidence and why.

Press **F1** for the full selector syntax.

### How to read the score

| Band | Score | Meaning |
|---|---|---|
| resilient | 75 to 100 | A test id, an automation id, a stable name, or an anchor. Safe to keep. |
| usable | 50 to 74 | It matches, but part of it may rot. Prefer a name or an anchor if you can. |
| fragile | 0 to 49 | An index, a generated id, `hwnd`, or a long path. Pick again or repair it. |

Things that lower the score: `:nth()`, an id that ends in a counter (`ok1`, `item-2`), a class name, `hwnd`, and a path with many steps.

Things that raise it: `data-testid` and the other test-id attributes, `automationid`, `aria-label`, `name`, visible `text`, and an anchor.

## Anchors

An anchor is not a parent/child step. The element is usually a sibling of the landmark. Spectra searches in the same window or browser tab and keeps the nearest match.

```
uia:Window[process="notepad.exe"] >> Text[name="Email"] /below/ Edit
web:tab[title="Shop"] >> label[text="Email"] /below/ button[text="OK"]
web:tab[host="shop.example"] >> button[text="OK"] /right/ button[text="Save"]
```

| Combinator | Meaning |
|---|---|
| `/below/` | Nearest element under the previous match, lined up with it |
| `/above/` | Nearest element above it |
| `/left/` | Nearest element to its left |
| `/right/` | Nearest element to its right |
| `/near/` | Nearest element within 96 pixels when it does not sit on a straight line |

Without `:nth()`, only the nearest match is used. `:nth(2)` is the second nearest. `:nth(-1)` is the farthest.

Both elements need an on-screen rectangle. If they share the same rectangle, Spectra keeps `:nth()`, because direction does not mean anything.

The generator writes an anchor when an index, or an id like `ok1`, would otherwise be the only way to tell two elements apart. A real id such as `save`, or a `data-testid`, is kept as it is.

## From Python

Start Spectra the usual way. Leave out `browser_port` for desktop-only use.

```python
from spectra.api import Spectra, grade_selector

s = Spectra(browser_port=9222)
```

### Grade a selector

`grade_selector` does not start the app and does not need Windows.

```python
grade = grade_selector('web:tab[title="Shop"] >> button:nth(2)')
print(grade.summary())       # fragile 28/100
print(grade.band)            # fragile, usable, or resilient
print(grade.score)           # 0 to 100
for reason in grade.reasons:
    print(reason)
```

Grade an element you already found:

```python
from spectra.core.resilience import grade_target

el = s.find('web:tab[title="Shop"] >> input[data-testid="email"]')
print(grade_target(el.node).summary())
```

`el.node` is the live element. Read it from the same thread Spectra uses for automation. `grade_selector` is the one to call from anywhere, because it only needs the selector text.

### Find by anchor

Write the combinator in the selector string. `find` waits until it matches or the timeout ends.

```python
field = s.find('web:tab[title="Shop"] >> label[text="Email"] /below/ input')
field.type_text("ada@example.com")

save = s.find('web:tab[title="Shop"] >> button[text="OK"] /right/ button[text="Save"]')
save.click()
```

The same strings work for desktop and Java. Use the `uia:` or `java:` prefix on the first step, as in the main selector guide.

### Repair a broken selector

```python
fixed = s.heal('web:tab[title="Shop"] >> button[text="Save changse"]')
print(fixed.selector)      # a selector that matches again
print(fixed.confidence)    # 0 to 1
print(fixed.why)           # one sentence
fixed.element.click()
```

`heal` raises `ElementNotFound` when nothing is close enough, or when two elements are equally likely. It will not guess.

Pass an element you already hold when the name on screen changed and the text no longer resembles anything:

```python
fixed = s.heal('uia:Window[process="notepad.exe"] >> Button[name="Save"]', hint=old_button)
```

If another element’s name is close to the text you typed, that element wins. The hint is only used when nothing else is close.

### Record a script

```python
rec = s.recording()
rec.click(s.find('web:tab[title="Shop"] >> label[text="Email"] /below/ button[text="OK"]'))
email = s.find('web:tab[title="Shop"] >> input[data-testid="email"]')
rec.type_text(email, "a")
rec.type_text(email, "b")          # same element: becomes type_text("ab")
rec.set_value(email, "ada@example.com")
rec.invoke(s.find('web:tab[title="Shop"] >> button[id="save"]'))
print(rec.to_python())
```

`to_python()` returns a script you can paste and run. Each step has a comment with its band and score. A web element stores the browser port it was found on. A desktop recording starts `Spectra()` with no port.

`click`, `type_text`, `set_value`, and `invoke` are the recorded actions. The selector stored for each one is the one the generator would write at that moment, including an anchor when that is the stable choice.

## Where the code lives

```
spectra/core/resilience.py   grade() and grade_target()
spectra/core/anchor.py       /below/ and the other combinators
spectra/core/heal.py         heal()
spectra/core/script.py       Recording
spectra/api.py               grade_selector(), Spectra.heal(), Spectra.recording()
spectra/builtin_plugins/target_quality.py   the Properties group
```

The selector syntax itself is parsed in `spectra/core/selector.py`. Search and generation, including the choice to use an anchor, are in `spectra/core/engine.py`.
