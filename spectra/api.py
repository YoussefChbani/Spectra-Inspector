"""Use Spectra selectors from your own Python scripts.

    from spectra.api import Spectra
    s = Spectra()                       # add browser_port=9222 to include a browser
    ok = s.find('uia:Window[process="notepad.exe"] >> Document')
    ok.click(); ok.type_text("hello")
"""
from __future__ import annotations

import ctypes
import sys
import time
from typing import List, Optional

from .core.node import Node
from .driver import Driver
from .service import Service


class ElementNotFound(LookupError):
    pass


class Element:
    def __init__(self, spectra: "Spectra", node: Node):
        self._s, self.node = spectra, node

    def _call(self, fn):
        return self._s.driver.call(fn)

    @property
    def tech(self) -> str:
        return self.node.tech

    def get(self, attr: str) -> Optional[str]:
        return self._call(lambda: self.node.get(attr))

    @property
    def name(self) -> Optional[str]:
        return self.get("name") if self.tech != "web" else self.get("text")

    @property
    def rect(self):
        return self._call(self.node.rect)

    def properties(self):
        return self._call(self.node.properties)

    def click(self, button: str = "left"):
        if self.tech == "web":
            return self._web_call("function(){this.scrollIntoView({block:'center'}); this.click();}")
        r = self.rect
        if not r:
            raise RuntimeError("element has no on-screen rectangle")
        _mouse_click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2, button)

    def type_text(self, text: str):
        if self.tech == "web":
            self._web_call("function(){this.focus();}")
            self.node.page.ensure().send("Input.insertText", {"text": text})
            return
        self.focus()
        _send_unicode(text)

    def focus(self):
        if self.tech == "uia":
            self._call(lambda: self.node.el.SetFocus())
        elif self.tech == "web":
            self._web_call("function(){this.focus();}")

    def set_value(self, value: str):
        if self.tech == "web":
            return self._web_call(
                "function(v){this.value=v;this.dispatchEvent(new Event('input',{bubbles:true}));"
                "this.dispatchEvent(new Event('change',{bubbles:true}));}", value)
        return self._call(lambda: self.node.backend.action(self.node, "Set value", value))

    def invoke(self):
        return self._call(lambda: self.node.backend.action(self.node, "Invoke"))

    def _web_call(self, fn: str, *args):
        conn = self.node.page.ensure()
        obj = conn.send("DOM.resolveNode", {"backendNodeId": self.node.bid})["object"]
        return conn.send("Runtime.callFunctionOn", {
            "objectId": obj["objectId"], "functionDeclaration": fn, "returnByValue": True,
            "arguments": [{"value": a} for a in args]}).get("result", {}).get("value")

    def __repr__(self):
        return f"<Element {self.node.label()}>"


class Spectra:
    def __init__(self, browser_port: Optional[int] = None, browser_host: str = "127.0.0.1"):
        self.service = Service()
        self.driver = Driver(init=self.service.init)
        self.driver.start()
        self.driver.ready.wait(30)
        if browser_port:
            self.driver.call(lambda: self.service.web.add_endpoint(browser_host, browser_port))

    def find_all(self, selector: str, timeout: float = 10.0) -> List[Element]:
        res = self.driver.call(lambda: self.service.engine.find_all(selector, timeout), timeout + 5)
        if res.error:
            raise RuntimeError(res.error)
        return [Element(self, n) for n in res.matches]

    def find(self, selector: str, timeout: float = 10.0, poll: float = 0.25) -> Element:
        deadline = time.monotonic() + timeout
        while True:
            left = max(0.5, deadline - time.monotonic())
            res = self.driver.call(lambda: self.service.engine.find_all(selector, left), left + 5)
            if res.matches:
                return Element(self, res.matches[0])
            if time.monotonic() >= deadline:
                raise ElementNotFound(f"no element matches {selector!r}" + (f" ({res.error})" if res.error else ""))
            time.sleep(poll)

    def exists(self, selector: str, timeout: float = 0.0) -> bool:
        try:
            self.find(selector, timeout)
            return True
        except ElementNotFound:
            return False

    def heal(self, selector: str, hint: Optional[Element] = None) -> "Healed":
        """Find what a broken selector was aiming at and rebuild it.

        ``hint`` is an element you already hold (the one that was selected). It
        breaks a tie. Raises ElementNotFound when nothing is close enough to
        trust.
        """
        node = hint.node if hint is not None else None
        hit = self.driver.call(lambda: self.service.engine.heal(selector, hint=node))
        if hit is None:
            raise ElementNotFound(f"could not heal {selector!r}")
        return Healed(Element(self, hit.node), hit.selector, hit.confidence, hit.why)

    def recording(self) -> "Recording":
        """Start a script. ``recording.click(element)`` stores the selector the generator would write."""
        from .core.script import Recording

        def generate(node):
            return self.driver.call(lambda: self.service.engine.generate(node)[0].to_text())

        return Recording(generate)


def grade_selector(selector: str):
    """Grade a selector without starting Spectra. See :class:`spectra.core.resilience.Grade`."""
    from .core.resilience import grade
    return grade(selector)


class Healed:
    """The element a broken selector was rebuilt to point at."""

    def __init__(self, element: Element, selector: str, confidence: float, why: str):
        self.element = element
        self.selector = selector
        self.confidence = confidence
        self.why = why

    def __repr__(self):
        return f"Healed({self.selector!r}, {self.confidence:.0%})"


# ------------------------------------------------------------------ input helpers (Windows)

def _mouse_click(x: int, y: int, button: str = "left"):
    if sys.platform != "win32":
        raise RuntimeError("mouse input needs Windows")
    u = ctypes.windll.user32
    u.SetCursorPos(x, y)
    down, up = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}[button]
    u.mouse_event(down, 0, 0, 0, 0)
    u.mouse_event(up, 0, 0, 0, 0)


def _send_unicode(text: str):
    if sys.platform != "win32":
        raise RuntimeError("keyboard input needs Windows")
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x4, 0x2
    for ch in text:
        units = [int.from_bytes(ch.encode("utf-16-le")[i:i + 2], "little")
                 for i in range(0, len(ch.encode("utf-16-le")), 2)]
        for unit in units:
            arr = (INPUT * 2)()
            for j, flags in enumerate((KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)):
                arr[j].type = 1
                arr[j].u.ki = KEYBDINPUT(0, unit, flags, 0, 0)
            ctypes.windll.user32.SendInput(2, arr, ctypes.sizeof(INPUT))
            time.sleep(0.002)
