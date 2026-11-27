"""Selector quality, relative anchors, self-healing and script recording.

These tests use the in-memory tree from test_core. They do not need Windows.
"""
import pytest

from spectra.builtin_plugins.target_quality import provider
from spectra.core.anchor import nearest_matches, relation_gap
from spectra.core.engine import Engine
from spectra.core.heal import heal
from spectra.core.resilience import grade, grade_target, looks_dynamic
from spectra.core.script import Recording
from spectra.core.selector import SelectorSyntaxError, parse
from tests.test_core import FakeBackend, FakeNode


class Box(FakeNode):
    def __init__(self, role, parent=None, rect=(0, 0, 10, 10), **attrs):
        super().__init__(role, parent, **attrs)
        self._rect = rect

    def rect(self):
        return self._rect


def _form():
    """A small dialog: an Email label, the field to its right, two OK buttons under it."""
    root = FakeNode("Desktop")
    window = Box("Window", root, rect=(0, 0, 400, 320), name="Notepad", process="notepad.exe")
    label = Box("Text", window, rect=(20, 30, 90, 52), name="Email")
    edit = Box("Edit", window, rect=(100, 30, 280, 52), automationid="email")
    ok1 = Box("Button", window, rect=(20, 70, 90, 100), name="OK")
    ok2 = Box("Button", window, rect=(110, 70, 180, 100), name="OK")
    eng = Engine()
    eng.register(FakeBackend(root))
    return eng, dict(window=window, label=label, edit=edit, ok1=ok1, ok2=ok2)


def test_looks_dynamic_matches_generated_ids():
    from spectra.core.resilience import looks_positional
    assert looks_dynamic("mui-12345")
    assert looks_dynamic("aabbccdd")
    assert looks_dynamic("react-abc")
    assert not looks_dynamic("email")
    assert looks_positional("ok1") and looks_positional("item-2")
    assert not looks_positional("save") and not looks_positional("h1") and not looks_positional("15")
    weak = grade('web:tab[title="Shop"] >> button[id="ok1"]')
    named = grade('web:tab[title="Shop"] >> button[id="save"]')
    assert any("counter" in reason for reason in weak.reasons)
    assert weak.score < named.score


def test_grade_rewards_a_test_id_and_punishes_nth():
    strong = grade('web:tab[host="github.com"] >> button[data-testid="save"]')
    weak = grade('uia:Window[process="notepad.exe"] >> Button:nth(2)')
    assert strong.band == "resilient"
    assert strong.score >= 75
    assert weak.band == "fragile"
    assert any(":nth" in reason for reason in weak.reasons)
    anchored = grade('uia:Window[name="Notepad"] >> Text[name="Email"] /below/ Button[name="OK"]')
    assert anchored.band == "resilient"
    assert any("Anchored" in reason for reason in anchored.reasons)
    assert anchored.score > weak.score


def test_grade_target_prefers_an_automation_id():
    named = FakeNode("Button", None, name="OK")
    identified = FakeNode("Button", None, automationid="save")
    plain = FakeNode("Pane", None)
    assert grade_target(named).band == "usable"
    assert grade_target(identified).band == "resilient"
    assert grade_target(plain).band == "fragile"
    rows = provider(identified)
    assert ("Target quality", "band", "resilient") in rows


def test_relation_selector_round_trip_and_rejects_unknown_relations():
    text = 'uia:Window[name="Notepad"] >> Text[name="Email"] /below/ Button[name="OK"]'
    sel = parse(text)
    assert sel.steps[2].relation == "below"
    assert sel.steps[1].deep and not sel.steps[2].deep
    assert sel.to_text() == text
    assert parse('Window /Below/ Button').steps[1].relation == "below"
    with pytest.raises(SelectorSyntaxError):
        parse("Button /under/ Edit")
    with pytest.raises(SelectorSyntaxError):
        parse("/below/ Button")


def test_below_is_the_nearest_and_nth_counts_outward():
    root = FakeNode("Desktop")
    window = Box("Window", root, rect=(0, 0, 200, 200), name="Dialog", process="app.exe")
    label = Box("Text", window, rect=(10, 10, 50, 30), name="Email")
    near = Box("Button", window, rect=(10, 40, 50, 60), name="OK")
    far = Box("Button", window, rect=(10, 80, 50, 100), name="OK")
    above = Box("Button", window, rect=(10, 0, 40, 8), name="OK")
    eng = Engine()
    eng.register(FakeBackend(root))
    sel = 'Window[name="Dialog"] >> Text[name="Email"] /below/ Button'
    assert eng.find_all(sel).matches == [near]
    assert eng.find_all(sel + ":nth(2)").matches == [far]
    assert above not in eng.find_all(sel).matches
    assert relation_gap("below", label, near) < relation_gap("below", label, far)


def test_a_button_under_a_wide_field_is_below_it():
    """A wide field must not be called 'to the left of' a button that sits underneath it."""
    root = FakeNode("Desktop")
    window = Box("Window", root, rect=(0, 0, 400, 300), name="Shop", process="app.exe")
    Box("Edit", window, rect=(100, 30, 320, 54), automationid="email", name="Email")
    under = Box("Button", window, rect=(110, 70, 180, 100), name="OK")
    Box("Button", window, rect=(20, 70, 90, 100), name="OK")
    eng = Engine()
    eng.register(FakeBackend(root))
    sel, res = eng.generate(under)
    assert res.matches == [under]
    assert sel.steps[-1].nth is None
    assert sel.steps[-1].relation
    # Beside a wide field is the bug. Under it, or to the right of the other button, is fine.
    if "Edit" in sel.to_text():
        assert sel.steps[-1].relation == "below"


def test_right_of_a_label_finds_the_field():
    eng, n = _form()
    found = eng.find_all('Window[name="Notepad"] >> Text[name="Email"] /right/ Edit')
    assert found.matches == [n["edit"]]


def test_a_tie_reports_both_elements():
    root = FakeNode("Desktop")
    window = Box("Window", root, rect=(0, 0, 200, 200), name="Dialog", process="app.exe")
    label = Box("Text", window, rect=(0, 0, 40, 20), name="Email")
    first = Box("Button", window, rect=(0, 30, 40, 50), name="OK")
    second = Box("Button", window, rect=(0, 31, 40, 51), name="OK")
    assert len(nearest_matches(label, [first, second], "below", None)) == 2


def test_generator_uses_an_anchor_instead_of_nth():
    eng, n = _form()
    sel, res = eng.generate(n["ok1"])
    assert res.matches == [n["ok1"]]
    assert sel.steps[-1].nth is None
    assert sel.steps[-1].relation == "below"
    assert sel.to_text() == (
        'uia:Window[process="notepad.exe"][name="Notepad"] >> Text[name="Email"] /below/ Button[name="OK"]')

    other, other_res = eng.generate(n["ok2"])
    assert other_res.matches == [n["ok2"]]
    assert other.steps[-1].relation == "right"
    assert other.to_text() == (
        'uia:Window[process="notepad.exe"][name="Notepad"] >> Button[name="OK"] /right/ Button[name="OK"]')


def test_heal_repairs_a_typo_and_refuses_an_ambiguous_selector():
    root = FakeNode("Desktop")
    window = FakeNode("Window", root, name="Notepad", process="notepad.exe")
    edit = FakeNode("Edit", window, automationid="15", name="Text editor")
    FakeNode("Edit", window, name="Search")
    eng = Engine()
    eng.register(FakeBackend(root))

    hit = heal(eng, 'uia:Window[name="Notepad"] >> Edit[name="Text editur"]')
    assert hit is not None and hit.node == edit
    assert hit.confidence >= 0.55
    assert "15" in hit.selector
    assert "close" in hit.why

    assert heal(eng, 'uia:Window[name="Notepad"] >> Button[name="OK"]') is None  # nothing like it
    exact = heal(eng, 'uia:Window[name="Notepad"] >> Edit[automationid="15"]')
    assert exact.confidence == 1.0 and exact.node == edit


def test_heal_does_not_let_the_selection_hide_a_better_name():
    root = FakeNode("Desktop")
    window = FakeNode("Window", root, name="Notepad", process="notepad.exe")
    selected = FakeNode("Button", window, name="OK")
    save = FakeNode("Button", window, name="Save changes")
    eng = Engine()
    eng.register(FakeBackend(root))
    hit = heal(eng, 'uia:Window[name="Notepad"] >> Button[name="Save changse"]', hint=selected)
    assert hit is not None and hit.node == save
    assert hit.node != selected


def test_heal_uses_the_selected_element_to_break_a_tie():
    root = FakeNode("Desktop")
    window = Box("Window", root, rect=(0, 0, 300, 200), name="Notepad", process="notepad.exe")
    first = Box("Button", window, rect=(10, 10, 60, 30), name="OK")
    second = Box("Button", window, rect=(80, 10, 140, 30), name="OK")
    eng = Engine()
    eng.register(FakeBackend(root))
    # The name "Save" matches neither button, so the selected one is the hint.
    hit = heal(eng, 'uia:Window[name="Notepad"] >> Button[name="Save"]', hint=second)
    assert hit is not None and hit.node == second
    assert hit.node != first


def test_recording_coalesces_typing_and_grades_the_selector():
    eng, n = _form()
    rec = Recording(lambda node: eng.generate(node)[0].to_text())
    rec.click(n["edit"])
    rec.type_text(n["edit"], "h")
    rec.type_text(n["edit"], "i")
    script = rec.to_python()
    assert script.count(".click()") == 1
    assert '.type_text("hi")' in script
    assert "from spectra.api import Spectra" in script
    assert "resilient" in script or "usable" in script

    class Wrapper:
        def __init__(self, node):
            self.node = node

    rec2 = Recording(lambda node: "Button")
    rec2.invoke(Wrapper(n["ok1"]))
    assert rec2.to_python().strip().endswith(".invoke()")


def test_nodes_without_rectangles_keep_nth():
    """The original generator behaviour, so identical rectangles do not grow an anchor."""
    root = FakeNode("Desktop")
    window = FakeNode("Window", root, name="Notepad", process="notepad.exe")
    FakeNode("Button", window, name="OK")
    second = FakeNode("Button", window, name="OK")
    eng = Engine()
    eng.register(FakeBackend(root))
    sel, res = eng.generate(second)
    assert res.matches == [second]
    assert sel.steps[-1].nth == 2
    assert sel.steps[-1].relation is None


def test_grade_selector_helper_does_not_need_a_running_app():
    from spectra.api import grade_selector
    assert grade_selector("Button:nth(2)").band == "fragile"
