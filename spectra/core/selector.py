r"""Spectra selector language: parsing, serialising and attribute matching.

A selector is a chain of *steps* joined by *combinators*::

    uia:Window[process="notepad.exe"][name$=" - Notepad"] >> Edit[automationid="15"]
    web:tab[host="github.com"] >> button[text="Sign in" i]
    java:frame[name="SwingSet"] >> push-button[name="OK"]:nth(2)
    uia:Window[process="notepad.exe"] >> Text[name="Email"] /below/ Edit

Step syntax:   [tech ":"] role-or-* filter* pseudo*
Combinators:   ">"  direct child          ">>" (or whitespace) any descendant
               /below/ /above/ /left/ /right/ /near/
                   the nearest element in that direction from the previous match,
                   searched in the same window or tab (not only its children).
                   Without :nth(), only the nearest is used. :nth(2) is the
                   second nearest. These need an on-screen rectangle.
Filters:       [attr]            attribute exists and is not empty
               [attr = "v"]      equals; * (any run) and ? (one character) are wildcards:
                                 [name="* - Notepad"]. Write \* or \? for a real star / question mark
               [attr != "v"]     not equal (same wildcards)
               [attr *= "v"]     contains
               [attr ^= "v"]     starts with
               [attr $= "v"]     ends with
               [attr ~= "re"]    regular expression (search)
               [attr ?= "a*b?"]  wildcard (same as =, kept for older selectors)
               trailing  i / s   case-insensitive / case-sensitive (default s)
Pseudos:       :nth(N)           N-th match (1-based, negative counts from the end)
               :visible          only elements with a non-empty on-screen rectangle

The tech prefix may only appear on the first step; every later step inherits it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

TECHS = ["uia", "web", "java"]  # plugins add their back ends' prefixes with register_tech()


def register_tech(tech: str):
    """Allow ``tech:`` as a selector prefix (called when a plugin adds a back end)."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", tech or ""):
        raise ValueError(f"invalid technology prefix {tech!r} (use lowercase letters, digits and _)")
    if tech not in TECHS:
        TECHS.append(tech)
OPS = ("!=", "*=", "^=", "$=", "~=", "?=", "=")


_WILD_CACHE: dict = {}


def _wild_tokens(pattern: str):
    """Yield ("*" | "?" | literal-char) for a value; \\* and \\? are a literal star / question mark."""
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n and pattern[i + 1] in "*?":
            yield ("lit", pattern[i + 1])
            i += 2
            continue
        yield ("wild", c) if c in "*?" else ("lit", c)
        i += 1


def has_wildcards(value: str) -> bool:
    """True when the value contains an unescaped * or ?."""
    return any(kind == "wild" for kind, _ in _wild_tokens(value))


def escape_wildcards(value: str) -> str:
    """Make a real attribute value safe to use with = (a star in a title stays a star)."""
    return value.replace("*", "\\*").replace("?", "\\?")


def unescape_wildcards(value: str) -> str:
    return "".join(c for _, c in _wild_tokens(value))


def _wild_re(pattern: str):
    """* = any run of characters, ? = one character; everything else literal
    (unlike fnmatch, [ and ] in window titles stay literal)."""
    rx = _WILD_CACHE.get(pattern)
    if rx is None:
        rx = re.compile("".join((".*" if c == "*" else ".") if kind == "wild" else re.escape(c)
                                for kind, c in _wild_tokens(pattern)), re.DOTALL)
        if len(_WILD_CACHE) > 512:
            _WILD_CACHE.clear()
        _WILD_CACHE[pattern] = rx
    return rx


class SelectorSyntaxError(ValueError):
    def __init__(self, msg: str, pos: int):
        super().__init__(f"{msg} (at character {pos + 1})")
        self.pos = pos


@dataclass
class Filter:
    name: str
    op: Optional[str] = None  # None means "exists"
    value: str = ""
    case_insensitive: bool = False

    @property
    def has_wildcards(self) -> bool:
        """'=' / '!=' / '?=' values may use * and ? as wildcards."""
        return has_wildcards(self.value)

    @property
    def literal(self) -> Optional[str]:
        """The exact value to look for when no wildcard is involved (escapes removed), else None."""
        return None if self.has_wildcards else unescape_wildcards(self.value)

    def matches(self, actual: Optional[str]) -> bool:
        if self.op is None:
            return bool(actual)
        a = "" if actual is None else str(actual)
        v = self.value
        if self.op == "~=":
            flags = re.IGNORECASE if self.case_insensitive else 0
            try:
                return re.search(v, a, flags) is not None
            except re.error:
                return False
        if self.case_insensitive:
            a, v = a.casefold(), v.casefold()
        if self.op in ("=", "!=", "?="):
            hit = _wild_re(v).fullmatch(a) is not None if has_wildcards(v) else a == unescape_wildcards(v)
            return not hit if self.op == "!=" else hit
        v = unescape_wildcards(v)  # \* means a literal star with every operator
        if self.op == "*=":
            return v in a
        if self.op == "^=":
            return a.startswith(v)
        if self.op == "$=":
            return a.endswith(v)
        return False

    def to_text(self) -> str:
        if self.op is None:
            return f"[{self.name}]"
        flag = " i" if self.case_insensitive else ""
        return f"[{self.name}{self.op}{quote(self.value)}{flag}]"


@dataclass
class Step:
    role: str = "*"
    filters: List[Filter] = field(default_factory=list)
    nth: Optional[int] = None
    visible: bool = False
    deep: bool = False  # combinator *before* this step: True = descendant, False = child
    tech: Optional[str] = None  # only meaningful on the first step
    relation: Optional[str] = None  # below/above/left/right/near, instead of > or >>

    def role_matches(self, role: str) -> bool:
        return self.role == "*" or self.role.casefold() == (role or "").casefold()

    def to_text(self) -> str:
        role = self.role if _is_ident(self.role) or self.role == "*" else quote(self.role)
        out = role + "".join(f.to_text() for f in self.filters)
        if self.visible:
            out += ":visible"
        if self.nth is not None:
            out += f":nth({self.nth})"
        return out


@dataclass
class Selector:
    steps: List[Step]
    included: Optional[List[int]] = None  # set by the generator: which ancestry levels are steps

    @property
    def tech(self) -> str:
        return (self.steps[0].tech if self.steps else None) or "uia"

    def to_text(self) -> str:
        parts = []
        for i, st in enumerate(self.steps):
            txt = st.to_text()
            if i == 0:
                prefix = ">> " if st.deep else ""
                parts.append(f"{prefix}{self.tech}:{txt}")
            elif st.relation:
                parts.append(f"/{st.relation}/ {txt}")
            else:
                parts.append((">> " if st.deep else "> ") + txt)
        return " ".join(parts)

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.to_text()


# --------------------------------------------------------------------------- helpers

_IDENT_RE = re.compile(r"[A-Za-z_#][A-Za-z0-9_.#\-]*")


def _is_ident(s: str) -> bool:
    return bool(s) and _IDENT_RE.fullmatch(s) is not None


def quote(value: str) -> str:
    # a backslash before * or ? is the wildcard escape and is written as is (\*), others are doubled
    v = re.sub(r"\\(?![*?])", r"\\\\", str(value))
    return '"' + v.replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


# --------------------------------------------------------------------------- parser

class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0

    def error(self, msg: str):
        raise SelectorSyntaxError(msg, self.i)

    def peek(self, n: int = 1) -> str:
        return self.s[self.i:self.i + n]

    def ws(self) -> bool:
        start = self.i
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1
        return self.i > start

    def eof(self) -> bool:
        return self.i >= len(self.s)

    def ident(self) -> str:
        m = _IDENT_RE.match(self.s, self.i)
        if not m:
            self.error("expected a name")
        self.i = m.end()
        return m.group(0)

    def string(self) -> str:
        q = self.s[self.i]
        self.i += 1
        out = []
        while True:
            if self.eof():
                self.error("unterminated string")
            c = self.s[self.i]
            if c == "\\":
                self.i += 1
                if self.eof():
                    self.error("unterminated escape")
                e = self.s[self.i]
                if e in "*?":
                    out.append("\\" + e)  # kept: \* and \? are a literal star / question mark in wildcards
                else:
                    out.append({"n": "\n", "t": "\t"}.get(e, e))
            elif c == q:
                self.i += 1
                return "".join(out)
            else:
                out.append(c)
            self.i += 1

    def value(self) -> str:
        if self.peek() in ("'", '"'):
            return self.string()
        start = self.i
        while not self.eof() and self.s[self.i] not in "] \t\r\n":
            self.i += 1
        if self.i == start:
            self.error("expected a value")
        return self.s[start:self.i]

    def step(self, first: bool) -> Step:
        st = Step()
        # optional tech prefix
        m = re.match(r"(" + "|".join(map(re.escape, TECHS)) + r"):(?!\()", self.s[self.i:])
        if m:
            if not first:
                self.error("a technology prefix is only allowed on the first step")
            st.tech = m.group(1)
            self.i += m.end()
        if self.peek() == "*":
            self.i += 1
        elif self.peek() in ("'", '"'):
            st.role = self.string()
        elif self.peek() in ("[", ":"):
            pass  # role omitted -> any
        else:
            st.role = self.ident()
        while not self.eof():
            c = self.peek()
            if c == "[":
                st.filters.append(self.filter())
            elif c == ":":
                self.pseudo(st)
            else:
                break
        return st

    def filter(self) -> Filter:
        self.i += 1  # [
        self.ws()
        name = self.ident()
        self.ws()
        if self.peek() == "]":
            self.i += 1
            return Filter(name)
        for op in OPS:
            if self.s.startswith(op, self.i):
                self.i += len(op)
                break
        else:
            self.error("expected an operator (=, !=, *=, ^=, $=, ~=, ?=)")
        self.ws()
        val = self.value()
        self.ws()
        ci = False
        if self.peek() in ("i", "I", "s", "S") and self.s[self.i + 1:self.i + 2] in ("]", " "):
            ci = self.peek().lower() == "i"
            self.i += 1
            self.ws()
        if self.peek() != "]":
            self.error("expected ']'")
        self.i += 1
        return Filter(name, op, val, ci)

    def pseudo(self, st: Step):
        self.i += 1  # :
        name = self.ident().lower()
        if name == "visible":
            st.visible = True
            return
        if name in ("nth", "index"):
            if self.peek() != "(":
                self.error("expected '('")
            self.i += 1
            m = re.match(r"\s*(-?\d+)\s*\)", self.s[self.i:])
            if not m:
                self.error("expected a number and ')'")
            n = int(m.group(1))
            if n == 0:
                self.error(":nth() is 1-based; use 1 for the first match or -1 for the last")
            st.nth = n
            self.i += m.end()
            return
        if name == "first":
            st.nth = 1
            return
        if name == "last":
            st.nth = -1
            return
        self.error(f"unknown pseudo ':{name}'")

    def _combinator(self, had_ws: bool):
        """Consume the combinator before the next step. Returns (deep, relation)."""
        if self.s.startswith(">>", self.i):
            self.i += 2
            return True, None
        if self.peek() == ">":
            self.i += 1
            return False, None
        if self.peek() == "/":
            m = re.match(r"/(below|above|left|right|near)/", self.s[self.i:], re.I)
            if not m:
                self.error("expected /below/, /above/, /left/, /right/ or /near/")
            self.i += m.end()
            return False, m.group(1).lower()
        if had_ws:
            return True, None
        self.error("unexpected character")

    def parse(self) -> Selector:
        steps: List[Step] = []
        self.ws()
        if self.eof():
            self.error("empty selector")
        deep, relation = False, None
        if self.s.startswith(">>", self.i):
            deep = True
            self.i += 2
            self.ws()
        elif self.peek() == ">":
            self.i += 1
            self.ws()
        elif self.peek() == "/":
            self.error("a relation needs an element before it")
        while True:
            st = self.step(first=not steps)
            st.deep = deep
            st.relation = relation
            steps.append(st)
            had_ws = self.ws()
            if self.eof():
                break
            deep, relation = self._combinator(had_ws)
            self.ws()
            if self.eof():
                self.error("selector ends with a combinator")
        if steps[0].tech is None:
            steps[0].tech = "uia"
        return Selector(steps)


def parse(text: str) -> Selector:
    return _Parser(text).parse()


def step_matches(step: Step, node) -> bool:
    """Does *node* satisfy the role and filters of *step* (nth not considered)?"""
    if not step.role_matches(node.role()):
        return False
    for f in step.filters:
        if not f.matches(node.get(f.name)):
            return False
    if step.visible:
        r = node.rect()
        if not r or r[2] <= r[0] or r[3] <= r[1]:
            return False
    return True
