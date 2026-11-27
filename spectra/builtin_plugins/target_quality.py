"""Shows, in Properties, whether the selected element has a stable handle."""
from spectra.plugins import PluginInfo

PLUGIN = PluginInfo(
    id="target_quality", name="Target quality", version="1.0",
    author="Spectra Inspector contributors",
    description="Adds a 'Target quality' group to the Properties panel: a score for how well this "
                "element can be found again (test id, automation id, accessible name) and why.")

GROUP = "Target quality"


def provider(node):
    from spectra.core.resilience import grade_target
    try:
        g = grade_target(node)
    except Exception:  # noqa: BLE001 - one bad element should not drop the whole property grid
        return [(GROUP, "score", "unavailable")]
    rows = [(GROUP, "band", g.band), (GROUP, "score", f"{g.score}/100")]
    rows += [(GROUP, "note", reason) for reason in g.reasons]
    return rows


def register(ctx):
    ctx.add_property_provider(provider)
