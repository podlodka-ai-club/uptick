from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "tools" / "trace-viewer"


class _ResourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.resources: list[tuple[str, str]] = []
        self.csp: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        resource = attributes.get("src") or attributes.get("href")
        if resource is not None:
            self.resources.append((tag, resource))
        if tag == "meta" and attributes.get("http-equiv") == "Content-Security-Policy":
            self.csp = attributes.get("content")


def test_trace_viewer_is_self_contained_and_denies_network() -> None:
    parser = _ResourceParser()
    parser.feed((VIEWER / "index.html").read_text())

    assert parser.resources == [
        ("link", "styles.css"),
        ("script", "core.js"),
        ("script", "app.js"),
    ]
    assert parser.csp is not None
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ):
        assert directive in parser.csp


def test_trace_viewer_uses_safe_dom_apis_only() -> None:
    javascript = "\n".join((VIEWER / name).read_text() for name in ("core.js", "app.js"))
    forbidden = (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "DOMParser",
        "eval(",
        "Function(",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "localStorage",
        "indexedDB",
    )

    assert "textContent" in javascript
    for token in forbidden:
        assert token not in javascript


def test_trace_viewer_does_not_depend_on_agent_runtime_or_package_manager() -> None:
    viewer_sources = "\n".join(path.read_text() for path in VIEWER.iterdir() if path.is_file())

    assert "uptick_agent" not in viewer_sources
    assert not (VIEWER / "package.json").exists()
    assert not (ROOT / ".openai" / "hosting.json").exists()


def test_decision_map_has_local_accessible_navigation_controls() -> None:
    html = (VIEWER / "index.html").read_text()
    javascript = (VIEWER / "app.js").read_text()

    for element_id in (
        "map-panel",
        "strategy-episodes",
        "decision-map-svg",
        "decision-map-minimap",
        "map-zoom-out",
        "map-zoom-in",
        "map-fit",
        "map-center",
        "map-expand-cadence",
        "decision-map-detail",
    ):
        assert f'id="{element_id}"' in html
    assert 'id="decision-map-svg"\n                role="group"' in html
    assert 'aria-label="Interactive map of saved decisions"' in html
    assert 'kind: "recorded_sequence"' in (VIEWER / "core.js").read_text()
    assert javascript.count('group.addEventListener("keydown"') == 2
    assert "setPointerCapture" in javascript
    assert "textContent" in javascript


def test_trace_viewer_supports_trace_v5_memory_and_sgr_v2() -> None:
    core = (VIEWER / "core.js").read_text()
    readme = (VIEWER / "README.md").read_text()
    html = (VIEWER / "index.html").read_text()
    javascript = (VIEWER / "app.js").read_text()

    assert "unsupported_sgr_envelope" in core
    assert "TRACE_SCHEMA_VERSION = 6" in core
    assert "SUPPORTED_TRACE_SCHEMA_VERSIONS" not in core
    assert "source_run_ids" not in core
    assert "learning_started" in core
    assert "retrieval_diagnostics" in core
    assert "SGR v2" in readme
    assert "legacy SGR v1 decision envelopes" in readme
    assert "rejected with a clear error" in readme
    assert 'id="timing-summary"' in html
    assert "Memory &amp; experiment" in html
    assert "Memory used for this decision" in javascript
    assert "Where wall time went" in javascript


def test_trace_viewer_defaults_to_decisions_tab_with_aria_tab_semantics() -> None:
    html = (VIEWER / "index.html").read_text()
    javascript = (VIEWER / "app.js").read_text()

    assert 'role="tablist"' in html
    assert html.count('role="tab"') == 4
    assert html.count('role="tabpanel"') == 4
    assert 'data-tab="run" aria-controls="run-panel" aria-selected="true"' in html
    assert 'tab: "run"' in javascript
    # The decision list is a selectable list; the selected row is marked, not styled only.
    assert 'role="list"' in html
    assert 'setAttribute("aria-current", "true")' in javascript
    # Large raw payloads render lazily on first open.
    assert 'addEventListener("toggle"' in javascript
    assert "timeline-header" not in html
    assert "Clear filters and open in Decisions" in javascript
    assert "if (outsideCurrentFilters)" in javascript
    assert "resetFilters(false)" in javascript
    assert "selectIteration(row.iteration, true, false)" in javascript
    assert "otherNativeControl" in javascript
    assert "Boolean(focusedTimelineRow)" in javascript
    assert ".timeline-row[aria-current='true']" in javascript


def test_viewer_displays_published_v2_outcomes_without_legacy_profit_panels() -> None:
    javascript = (VIEWER / "app.js").read_text()
    html = (VIEWER / "index.html").read_text()
    assert 'statusCard("Score"' in javascript
    assert 'statusCard("Uptime"' in javascript
    assert 'moneyCard("Total cost"' in javascript
    assert "profit-chart-svg" not in html
    for text in ("Final balance", "Operating result", "Lost opportunity", "Purchase observations"):
        assert text not in javascript


def test_viewer_comparison_uses_current_score_and_cost_fields() -> None:
    javascript = (VIEWER / "app.js").read_text()
    assert "comparison.median_candidate_minus_baseline_score" in javascript
    assert "comparison.median_candidate_minus_baseline_cost_minor" in javascript
    assert "comparison.median_candidate_minus_baseline_minor" not in javascript
    assert "comparison.p10_candidate_minus_baseline_minor" not in javascript
    assert "summary.json schema v5" in javascript
