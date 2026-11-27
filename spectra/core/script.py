"""Turn a series of elements into a Spectra Python script.

This is the recording model Playwright's codegen is built on: each action
stores the selector at the moment it happened, consecutive typing on the same
element becomes one ``type_text`` call, and the selector is the one the
generator would write (an anchor when that is more stable than ``:nth()``).

The GUI does not have to be open. Pass any callable that turns a node into a
selector string, usually ``lambda node: engine.generate(node)[0].to_text()``.
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from .node import Node
from .resilience import grade

Generate = Callable[[Node], str]


class Recording:
    """A script being built from elements."""

    def __init__(self, generate: Generate):
        self._generate = generate
        self._steps: List[dict] = []
        self._port: Optional[int] = None

    def click(self, element) -> None:
        self._add("click", element, "")

    def type_text(self, element, text: str) -> None:
        """Append characters. A second call on the same element extends the previous one."""
        node = _unwrap(element)
        selector, band, score = self._describe(node)
        if self._steps and self._steps[-1]["action"] == "type_text" and self._steps[-1]["selector"] == selector:
            self._steps[-1]["value"] += text
            return
        self._steps.append({"action": "type_text", "selector": selector, "value": text,
                            "band": band, "score": score})

    def set_value(self, element, value: str) -> None:
        self._add("set_value", element, value)

    def invoke(self, element) -> None:
        self._add("invoke", element, "")

    def to_python(self, browser_port: Optional[int] = None) -> str:
        """A script that replays the recording with ``spectra.api.Spectra``."""
        port = browser_port if browser_port is not None else self._port
        head = "browser_port=%d" % port if port else ""
        lines = ["from spectra.api import Spectra", f"s = Spectra({head})", ""]
        for step in self._steps:
            sel = step["selector"].replace("'''", "\\'\\'\\'")
            lines.append(f"# {step['band']} {step['score']}/100")
            call = f"s.find(r'''{sel}''')"
            action = step["action"]
            if action == "type_text":
                lines.append(f"{call}.type_text({json.dumps(step['value'])})")
            elif action == "set_value":
                lines.append(f"{call}.set_value({json.dumps(step['value'])})")
            else:
                lines.append(f"{call}.{action}()")
        return "\n".join(lines) + ("\n" if lines else "")

    def _add(self, action: str, element, value: str) -> None:
        node = _unwrap(element)
        selector, band, score = self._describe(node)
        self._steps.append({"action": action, "selector": selector, "value": value,
                            "band": band, "score": score})

    def _describe(self, node: Node):
        selector = self._generate(node)
        g = grade(selector)
        page = getattr(node, "page", None)
        endpoint = getattr(page, "endpoint", None)
        port = getattr(endpoint, "port", None)
        if isinstance(port, int):
            self._port = port
        return selector, g.band, g.score


def _unwrap(element) -> Node:
    if isinstance(element, Node):
        return element
    node = getattr(element, "node", None)
    if isinstance(node, Node):
        return node
    raise TypeError("expected a Spectra element or node")
