"""Web back end: inspects live browser pages over the DevTools protocol.

Model::

    web root
      tab[title, url, host]            one per open page of each attached browser
        html                           the page's documentElement
          body ... elements            shadow roots and same-process iframes are
                                       flattened in, so ">>" crosses them

No browser extension is needed; the browser only has to expose a debugging
port (``--remote-debugging-port``). Spectra can launch one for you.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..core.node import Backend, Node, Rect
from ..core.resilience import looks_dynamic, looks_positional
from .cdp import CdpConnection, CdpError, list_targets, port_open, probe

TEST_ID_ATTRS = ("data-testid", "data-test-id", "data-test", "data-qa", "data-cy", "data-automation-id")
PREFERRED_ATTRS = TEST_ID_ATTRS + ("id", "name", "aria-label", "placeholder", "title", "alt", "type",
                                   "role", "href", "value", "for", "class")
TEXT_ROLES = {"a", "button", "label", "span", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
              "option", "p", "summary", "legend", "strong", "b", "em", "div", "dt", "dd", "caption"}
SKIP_TEXT = {"script", "style", "noscript", "template", "head"}

ScreenMapper = Callable[["WebPage"], Optional[Tuple[float, float, float]]]


class Endpoint:
    def __init__(self, host: str, port: int, label: str = ""):
        self.host, self.port = host, port
        self.label = label or f"{host}:{port}"
        self.browser = ""

    def __repr__(self):
        return f"Endpoint({self.host}:{self.port})"


class WebPage:
    """A connected page target with a cached DOM snapshot."""

    def __init__(self, backend: "WebBackend", endpoint: Endpoint, target: dict):
        self.backend = backend
        self.endpoint = endpoint
        self.target = target
        self.target_id = target["id"]
        self.conn: Optional[CdpConnection] = None
        self._lock = threading.RLock()
        self._dirty = True
        self._snap_time = 0.0
        self.nodes: Dict[int, "WebDomNode"] = {}  # backendNodeId -> node
        self.raw_by_node_id: Dict[int, dict] = {}
        self.doc_element: Optional["WebDomNode"] = None
        self._inspect_cb: Optional[Callable[[Optional["WebDomNode"]], None]] = None
        self.tab_node = WebTabNode(self)

    # --- target metadata -----------------------------------------------------
    @property
    def title(self) -> str:
        return self.target.get("title", "")

    @property
    def url(self) -> str:
        return self.target.get("url", "")

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""

    # --- connection ------------------------------------------------------------
    def ensure(self) -> CdpConnection:
        with self._lock:
            if self.conn is None or self.conn.closed:
                ws = self.target.get("webSocketDebuggerUrl")
                if not ws:
                    raise CdpError("this tab is already being debugged by another client (close DevTools on it)")
                self.conn = CdpConnection(ws)
                self.conn.on_event(self._on_event)
                self.conn.send("DOM.enable")
                try:
                    self.conn.send("Overlay.enable")
                except CdpError:
                    pass
                self._dirty = True
            return self.conn

    def _on_event(self, method: str, params: dict):
        if method.startswith("DOM.") and method not in ("DOM.setChildNodes",):
            self._dirty = True
        elif method == "Overlay.inspectNodeRequested":
            cb, self._inspect_cb = self._inspect_cb, None
            if cb:
                try:
                    self.conn.send("Overlay.setInspectMode", {"mode": "none", "highlightConfig": {}})
                except CdpError:
                    pass
                node = self.node_by_backend_id(params.get("backendNodeId"), refresh=True)
                cb(node)
        elif method in ("Page.frameNavigated", "Inspector.detached"):
            self._dirty = True

    def close(self):
        if self.conn:
            self.conn.close()

    # --- DOM snapshot ----------------------------------------------------------
    def snapshot(self, force: bool = False) -> Optional["WebDomNode"]:
        with self._lock:
            conn = self.ensure()
            if not force and not self._dirty and self.doc_element is not None:
                return self.doc_element
            doc = conn.send("DOM.getDocument", {"depth": -1, "pierce": True}, timeout=30)["root"]
            self._dirty = False
            self._snap_time = time.monotonic()
            old = self.nodes
            self.nodes = {}
            self.raw_by_node_id = {}
            self.doc_element = self._build(doc, None, old)
            return self.doc_element

    def _build(self, doc: dict, parent_for_root, old) -> Optional["WebDomNode"]:
        html_raw = next((c for c in doc.get("children", []) if c.get("nodeType") == 1), None)
        if html_raw is None:
            return None
        root = self._make(html_raw, parent_for_root, old)
        stack = [root]
        while stack:
            node = stack.pop()
            kids = []
            for raw in self._child_raws(node.raw):
                if raw.get("nodeType") == 9:  # iframe content document
                    sub = next((c for c in raw.get("children", []) if c.get("nodeType") == 1), None)
                    if sub is not None:
                        kids.append(self._make(sub, node, old))
                elif raw.get("nodeType") == 1:
                    kids.append(self._make(raw, node, old))
            node._kids = kids
            stack.extend(kids)
        return root

    @staticmethod
    def _child_raws(raw: dict) -> List[dict]:
        out = []
        for sr in raw.get("shadowRoots", []) or []:
            if sr.get("shadowRootType") != "user-agent":
                out.extend(sr.get("children", []) or [])
        out.extend(raw.get("children", []) or [])
        if raw.get("contentDocument"):
            out.append(raw["contentDocument"])
        return out

    def _make(self, raw: dict, parent, old) -> "WebDomNode":
        bid = raw["backendNodeId"]
        node = old.get(bid)
        if node is None:
            node = WebDomNode(self, raw, parent)
        else:
            node._reset(raw, parent)
        self.nodes[bid] = node
        self.raw_by_node_id[raw["nodeId"]] = raw
        return node

    def node_by_backend_id(self, bid: Optional[int], refresh: bool = False) -> Optional["WebDomNode"]:
        if bid is None:
            return None
        self.snapshot()
        node = self.nodes.get(bid)
        if node is None and refresh:
            self.snapshot(force=True)
            node = self.nodes.get(bid)
        return node

    # --- geometry, highlight, picking ------------------------------------------
    def css_box(self, bid: int) -> Optional[Tuple[float, float, float, float]]:
        try:
            model = self.ensure().send("DOM.getBoxModel", {"backendNodeId": bid})["model"]
        except CdpError:
            return None
        q = model["border"]
        xs, ys = q[0::2], q[1::2]
        return min(xs), min(ys), max(xs), max(ys)

    def page_metrics(self) -> dict:
        return self.ensure().evaluate(
            "({sx:screenX,sy:screenY,ow:outerWidth,oh:outerHeight,iw:innerWidth,ih:innerHeight,"
            "dpr:devicePixelRatio,vis:document.visibilityState,focus:document.hasFocus(),"
            "title:document.title,url:location.href})") or {}

    def screen_origin(self) -> Optional[Tuple[float, float, float]]:
        """(left, top, scale) of the page viewport in physical screen pixels."""
        if self.backend.screen_mapper:
            try:
                r = self.backend.screen_mapper(self)
                if r:
                    return r
            except Exception:  # noqa: BLE001
                pass
        try:
            m = self.page_metrics()
        except CdpError:
            return None
        if not m:
            return None
        dpr = float(m.get("dpr") or 1)
        border = max(0.0, (m["ow"] - m["iw"]) / 2)
        top_ui = max(0.0, m["oh"] - m["ih"] - border)
        return ((m["sx"] + border) * dpr, (m["sy"] + top_ui) * dpr, dpr)

    def css_to_screen(self, box) -> Optional[Rect]:
        origin = self.screen_origin()
        if not origin or not box:
            return None
        ox, oy, s = origin
        return (int(ox + box[0] * s), int(oy + box[1] * s), int(ox + box[2] * s), int(oy + box[3] * s))

    def highlight(self, bid: Optional[int]):
        conn = self.ensure()
        if bid is None:
            conn.send("Overlay.hideHighlight")
            return
        conn.send("Overlay.highlightNode", {"backendNodeId": bid, "highlightConfig": HIGHLIGHT})

    def start_inspect(self, callback: Callable[[Optional["WebDomNode"]], None]):
        self._inspect_cb = callback
        self.ensure().send("Overlay.setInspectMode", {"mode": "searchForNode", "highlightConfig": HIGHLIGHT})

    def stop_inspect(self):
        self._inspect_cb = None
        try:
            self.ensure().send("Overlay.setInspectMode", {"mode": "none", "highlightConfig": {}})
        except CdpError:
            pass

    def node_at_css(self, x: float, y: float) -> Optional["WebDomNode"]:
        r = self.ensure().send("DOM.getNodeForLocation", {"x": int(x), "y": int(y),
                                                           "includeUserAgentShadowDOM": False,
                                                           "ignorePointerEventsNone": True})
        node = self.node_by_backend_id(r.get("backendNodeId"), refresh=True)
        # text nodes resolve to their parent element
        return node

    def bring_to_front(self):
        self.ensure().send("Page.bringToFront")

    def ax_info(self, bid: int) -> dict:
        try:
            nodes = self.ensure().send("Accessibility.getPartialAXTree",
                                       {"backendNodeId": bid, "fetchRelatives": False})["nodes"]
        except CdpError:
            return {}
        if not nodes:
            return {}
        n = nodes[0]
        out = {"role": (n.get("role") or {}).get("value", ""), "name": (n.get("name") or {}).get("value", "")}
        for p in n.get("properties", []) or []:
            out[p["name"]] = (p.get("value") or {}).get("value", "")
        return out

    def count_css(self, css: str) -> int:
        return int(self.ensure().evaluate(f"document.querySelectorAll({_js(css)}).length"))

    def count_xpath(self, xp: str) -> int:
        return int(self.ensure().evaluate(
            f"document.evaluate({_js(xp)},document,null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null).snapshotLength"))


def _js(s: str) -> str:
    import json
    return json.dumps(s)


HIGHLIGHT = {
    "showInfo": True, "showStyles": False, "showAccessibilityInfo": True,
    "contentColor": {"r": 20, "g": 184, "b": 166, "a": 0.25},
    "paddingColor": {"r": 45, "g": 212, "b": 191, "a": 0.2},
    "borderColor": {"r": 13, "g": 148, "b": 136, "a": 0.9},
    "marginColor": {"r": 153, "g": 246, "b": 228, "a": 0.15},
}


# ----------------------------------------------------------------------------- nodes

class WebRootNode(Node):
    tech = "web"

    def __init__(self, backend: "WebBackend"):
        self.backend = backend

    def key(self):
        return ("root",)

    def role(self):
        return "browsers"

    def label(self):
        return "Browsers (DevTools)"

    def children(self):
        return [p.tab_node for p in self.backend.pages()]

    def parent(self):
        return None


class WebTabNode(Node):
    tech = "web"

    def __init__(self, page: WebPage):
        self.page = page

    def key(self):
        return ("tab", self.page.endpoint.port, self.page.target_id)

    def role(self):
        return "tab"

    def selector_attrs(self):
        return {"title": self.page.title, "url": self.page.url, "host": self.page.host,
                "port": str(self.page.endpoint.port)}

    def suggested_attrs(self, is_top=False):
        return ["host", "title"]

    def label(self):
        return f"tab  \"{self.page.title[:60]}\"  {self.page.host}"

    def properties(self):
        rows = [("Tab", k, v) for k, v in self.selector_attrs().items()]
        rows.append(("Tab", "target id", self.page.target_id))
        rows.append(("Tab", "browser", self.page.endpoint.browser))
        return rows

    def children(self):
        root = self.page.snapshot()
        return [root] if root else []

    def parent(self):
        return self.page.backend.root()

    def rect(self):
        try:
            m = self.page.page_metrics()
            return self.page.css_to_screen((0, 0, m["iw"], m["ih"]))
        except Exception:  # noqa: BLE001
            return None


class WebDomNode(Node):
    tech = "web"

    def __init__(self, page: WebPage, raw: dict, parent):
        self.page = page
        self._kids: List[WebDomNode] = []
        self._reset(raw, parent)

    def _reset(self, raw, parent):
        self.raw = raw
        self.bid = raw["backendNodeId"]
        self._parent = parent
        self._attrs: Optional[dict] = None
        self._text: Optional[str] = None
        self._rect_known = False
        self._rect_cache = None

    def key(self):
        return ("el", self.page.endpoint.port, self.page.target_id, self.bid)

    def role(self):
        return (self.raw.get("localName") or self.raw.get("nodeName", "")).lower()

    def html_attrs(self) -> dict:
        a = self.raw.get("attributes") or []
        return {a[i]: a[i + 1] for i in range(0, len(a) - 1, 2)}

    def text(self) -> str:
        if self._text is None:
            parts: List[str] = []
            budget = [400]

            def visit(raw):
                if budget[0] <= 0:
                    return
                t = raw.get("nodeType")
                if t == 3:
                    v = raw.get("nodeValue", "")
                    parts.append(v)
                    budget[0] -= len(v)
                elif t == 1 and (raw.get("localName") or "") not in SKIP_TEXT:
                    for c in raw.get("children", []) or []:
                        visit(c)
                    for sr in raw.get("shadowRoots", []) or []:
                        if sr.get("shadowRootType") != "user-agent":
                            for c in sr.get("children", []) or []:
                                visit(c)

            visit(self.raw)
            self._text = re.sub(r"\s+", " ", "".join(parts)).strip()[:200]
        return self._text

    def own_text(self) -> str:
        return re.sub(r"\s+", " ", "".join(c.get("nodeValue", "") for c in self.raw.get("children", []) or []
                                          if c.get("nodeType") == 3)).strip()

    def selector_attrs(self):
        if self._attrs is None:
            html = self.html_attrs()
            out = {}
            for k in PREFERRED_ATTRS:
                if k in html:
                    out[k] = html[k]
            text = self.text()
            if text:
                out["text"] = text
            for k, v in html.items():
                if k not in out and k != "style":
                    out[k] = v
            if any(sr.get("shadowRootType") != "user-agent" for sr in self.raw.get("shadowRoots") or []):
                out["shadow-host"] = "true"
            if self.raw.get("frameId"):
                out["frame-id"] = self.raw["frameId"]
            self._attrs = out
        return self._attrs

    def get(self, name):
        if name == "tag":
            return self.role()
        if name == "own-text":
            return self.own_text()
        return self.selector_attrs().get(name)

    def suggested_attrs(self, is_top=False):
        a = self.selector_attrs()
        for k in TEST_ID_ATTRS:
            if a.get(k):
                return [k]
        # ok1 / item-2 follow position. The visible name, then an anchor, survives a reorder.
        if a.get("id") and not looks_dynamic(a["id"]) and not looks_positional(a["id"]):
            return ["id"]
        out = []
        for k in ("name", "aria-label", "placeholder", "title", "alt", "for"):
            if a.get(k) and len(out) < 2:
                out.append(k)
        if self.role() == "input" and a.get("type") and len(out) < 2:
            out.append("type")
        if not out and a.get("role"):
            out.append("role")
        t = a.get("text", "")
        if t and len(t) <= 50 and self.role() in TEXT_ROLES and len(self._kids) <= 3 and len(out) < 2:
            out.append("text")
        if not out and self.role() == "a" and a.get("href") and len(a["href"]) < 120:
            out.append("href")
        return out

    def label(self):
        a = self.selector_attrs()
        bits = [self.role()]
        if a.get("id"):
            bits.append("#" + a["id"])
        if a.get("class"):
            bits.append("." + ".".join(a["class"].split()[:2]))
        t = self.own_text() or (a.get("aria-label") or a.get("placeholder") or a.get("name") or a.get("title")
                                or a.get("type") or "")
        if t:
            bits.append(f"  \"{t[:50]}\"")
        return "".join(bits[:3]) + (bits[3] if len(bits) > 3 else "")

    def properties(self):
        rows = [("Element", "tag", self.role()), ("Element", "backend node id", str(self.bid))]
        for k, v in self.html_attrs().items():
            rows.append(("HTML attributes", k, v))
        rows.append(("Element", "text", self.text()))
        box = self.page.css_box(self.bid)
        if box:
            rows.append(("Geometry", "css rect (x, y, w, h)",
                         f"{box[0]:.0f}, {box[1]:.0f}, {box[2]-box[0]:.0f}, {box[3]-box[1]:.0f}"))
            r = self.page.css_to_screen(box)
            if r:
                rows.append(("Geometry", "screen rect (l, t, r, b)", ", ".join(map(str, r))))
        for k, v in self.page.ax_info(self.bid).items():
            rows.append(("Accessibility", k, str(v)))
        from ..core.webpaths import css_path, xpath
        rows.append(("Locators", "css", css_path(self)))
        rows.append(("Locators", "xpath", xpath(self)))
        rows.append(("Page", "title", self.page.title))
        rows.append(("Page", "url", self.page.url))
        return rows

    def children(self):
        return self._kids

    def parent(self):
        return self._parent if self._parent is not None else self.page.tab_node

    def rect(self):
        # Box model calls are cross-process. Anchors ask for the same rectangle many times.
        if not self._rect_known:
            box = self.page.css_box(self.bid)
            self._rect_cache = self.page.css_to_screen(box) if box else None
            self._rect_known = True
        return self._rect_cache

    def in_shadow_or_frame(self) -> bool:
        node = self
        while isinstance(node, WebDomNode):
            par = node._parent
            if par is None:
                return False
            if node.raw.get("parentId") != par.raw.get("nodeId"):
                return True
            node = par
        return False


# ----------------------------------------------------------------------------- backend

class WebBackend(Backend):
    tech = "web"
    title = "Browsers (DevTools)"

    def __init__(self):
        self.endpoints: List[Endpoint] = []
        self._pages: Dict[Tuple[int, str], WebPage] = {}
        self._root = WebRootNode(self)
        self._last_refresh = 0.0
        self.screen_mapper: Optional[ScreenMapper] = None
        self._lock = threading.RLock()

    def root(self):
        return self._root

    def add_endpoint(self, host: str = "127.0.0.1", port: int = 9222) -> Optional[Endpoint]:
        info = probe(host, port, timeout=2)
        if info is None:
            return None
        with self._lock:
            for ep in self.endpoints:
                if ep.host == host and ep.port == port:
                    ep.browser = info.get("Browser", "")
                    return ep
            ep = Endpoint(host, port)
            ep.browser = info.get("Browser", "")
            self.endpoints.append(ep)
        self.refresh(force=True)
        return ep

    def discover(self, ports=(9222, 9223, 9224, 9225, 9229, 9333)) -> List[Endpoint]:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(len(ports) or 1) as ex:  # check every port at once
            open_ports = [p for p, ok in zip(ports, ex.map(lambda p: port_open("127.0.0.1", p), ports)) if ok]
        found = []
        for p in open_ports:
            ep = self.add_endpoint("127.0.0.1", p)
            if ep:
                found.append(ep)
        return found

    def refresh(self, force: bool = False):
        with self._lock:
            if not force and time.monotonic() - self._last_refresh < 1.0:
                return
            self._last_refresh = time.monotonic()
            alive = set()
            for ep in list(self.endpoints):
                if not port_open(ep.host, ep.port):
                    continue  # browser closed: don't wait for the HTTP timeout
                try:
                    targets = list_targets(ep.host, ep.port)
                except Exception:  # noqa: BLE001
                    continue
                for t in targets:
                    if t.get("type") not in ("page", "webview", "app"):
                        continue
                    k = (ep.port, t["id"])
                    alive.add(k)
                    page = self._pages.get(k)
                    if page is None:
                        self._pages[k] = WebPage(self, ep, t)
                    else:
                        if t.get("url") != page.target.get("url"):
                            page._dirty = True
                        page.target.update({kk: vv for kk, vv in t.items() if kk != "webSocketDebuggerUrl"})
            for k in list(self._pages):
                if k not in alive:
                    self._pages.pop(k).close()

    def pages(self) -> List[WebPage]:
        self.refresh()
        return list(self._pages.values())

    def page_for(self, port: int, target_id: str) -> Optional[WebPage]:
        return self._pages.get((port, target_id))
