#!/usr/bin/env python3
"""Static EDL conformance checker for AgentIT portal Jinja templates.

Walks ``src/agentit/portal/templates/**/*.html`` and reports violations of
``docs/portal-experience-design-language.md`` rules tagged [check].

Exit codes:
  0 — no MUST violations
  1 — one or more MUST violations (SHOULD are warnings printed to stderr)

Usage:
  uv run python scripts/check_portal_edl.py
  uv run python scripts/check_portal_edl.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "src" / "agentit" / "portal" / "templates"

_STATUS_IN_BTN = re.compile(
    r"(no dry run yet|warning|failed|error|pending|please wait|"
    r"this can take|don.?t close)",
    re.I,
)

_NATIVE_CONFIRM = re.compile(
    r"""(?:window\.confirm\s*\(|(?<![\w.])confirm\s*\(\s*['"])""",
)

_HEX_INLINE = re.compile(
    r"""style\s*=\s*["'][^"']*#[0-9a-fA-F]{3,8}""",
)

_BADGE_FONT = re.compile(
    r"""\.badge\s*\{[^}]*font-size\s*:\s*([^;}+]+)""",
    re.S,
)


@dataclass
class Violation:
    rule: str
    severity: str  # MUST | SHOULD
    path: str
    line: int
    message: str


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


class _ClickScopeParser(HTMLParser):
    """Track open tags that carry x-data so @click ancestors can be checked."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[bool] = []
        self.violations: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str | None, str | None]]) -> None:
        attr_map = {(k or ""): (v or "") for k, v in attrs}
        has_xdata = "x-data" in attr_map
        in_scope = has_xdata or (self.stack[-1] if self.stack else False)
        void = tag.lower() in {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }
        click_keys = [
            k for k in attr_map
            if k in ("@click", "x-on:click")
            or k.startswith("@click.")
            or k.startswith("x-on:click")
        ]
        if click_keys and not in_scope and not has_xdata:
            self.violations.append(
                (self.getpos()[0], f"<{tag}> has {click_keys[0]} outside x-data scope")
            )
        if not void:
            self.stack.append(has_xdata or in_scope)

    def handle_endtag(self, tag: str) -> None:
        if self.stack:
            self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str | None, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _strip_jinja(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    text = re.sub(r"\{\{.*?\}\}", "", text, flags=re.S)
    text = re.sub(r"\{%.*?%\}", "", text, flags=re.S)
    return text


def _iter_buttons(html: str) -> list[tuple[int, str, str]]:
    """Return (line, open_attrs, inner_html) for real HTML <button> elements."""
    out: list[tuple[int, str, str]] = []
    for m in re.finditer(r"<button(?:\s([^>]*))?>([\s\S]*?)</button>", html, re.I):
        before = html[: m.start()]
        if before.count("/*") > before.count("*/"):
            continue
        out.append((_line_of(html, m.start()), m.group(1) or "", m.group(2)))
    return out


def _badge_font_ok(decl: str) -> bool:
    decl = decl.strip()
    if "var(--font-xs)" in decl or "var(--font-sm)" in decl or "var(--font-base)" in decl:
        return True
    m = re.match(r"([\d.]+)\s*rem", decl)
    if m:
        return float(m.group(1)) >= 0.75 - 1e-9
    m = re.match(r"([\d.]+)\s*px", decl)
    if m:
        return float(m.group(1)) >= 12 - 1e-9
    return False


def check_file(path: Path) -> list[Violation]:
    text = path.read_text(encoding="utf-8")
    rel = _rel(path)
    vios: list[Violation] = []

    for m in _NATIVE_CONFIRM.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.start())
        line = text[line_start: line_end if line_end != -1 else len(text)]
        if "//" in line and line.find("//") < (m.start() - line_start):
            continue
        if "{#" in line:
            continue
        vios.append(Violation(
            "EDL-NO-NATIVE-CONFIRM", "MUST", rel, _line_of(text, m.start()),
            "native confirm() is forbidden; use #confirm-modal / show-confirm",
        ))

    for m in re.finditer(r"<a\b[^>]*\bhref\s*=\s*([\"'])(.*?)\1[^>]*>", text, re.I | re.S):
        href = m.group(2)
        if "pr_url" in href and "safe_url" not in href:
            vios.append(Violation(
                "EDL-SAFE-URL", "MUST", rel, _line_of(text, m.start()),
                "href with pr_url must use | safe_url",
            ))

    for line_no, open_attrs, inner in _iter_buttons(text):
        # Discrete `.badge` / `badge-*` classes only — not `events-bell-badge`.
        nested_status_badge = False
        for class_attr in re.findall(r"""class\s*=\s*["']([^"']*)["']""", inner, re.I):
            tokens = class_attr.split()
            if any(t == "badge" or t.startswith("badge-") for t in tokens):
                nested_status_badge = True
                break
        if nested_status_badge:
            vios.append(Violation(
                "EDL-BTN-STATUS", "MUST", rel, line_no,
                "status badge must not be nested inside <button>; place beside the control",
            ))
            continue
        plain = re.sub(r"<[^>]+>", " ", inner)
        plain = re.sub(r"\{\{.*?\}\}", " ", plain, flags=re.S)
        plain = re.sub(r"\{%.*?%\}", " ", plain, flags=re.S)
        plain = re.sub(r"\s+", " ", plain).strip()
        if "htmx-indicator" in inner or "spinner" in inner:
            plain = re.sub(r"(Running|Assessing|Saving|Working).*$", "", plain, flags=re.I).strip()
        # Icon-only notification/menu controls (aria-label / events-bell).
        if "aria-label" in open_attrs or ":aria-label" in open_attrs or "events-bell" in open_attrs:
            continue
        if _STATUS_IN_BTN.search(plain):
            vios.append(Violation(
                "EDL-BTN-STATUS", "MUST", rel, line_no,
                f"status/warning copy inside button: {plain[:80]!r}",
            ))
        elif len(plain) > 48:
            vios.append(Violation(
                "EDL-BTN-STATUS", "SHOULD", rel, line_no,
                f"button label is long ({len(plain)} chars); prefer ≤3 words: {plain[:80]!r}",
            ))

    # Macros may document caller-provided Alpine scope (see client_tab_nav).
    if path.name != "_macros.html":
        parser = _ClickScopeParser()
        try:
            parser.feed(_strip_jinja(text))
        except Exception as exc:  # noqa: BLE001
            vios.append(Violation(
                "EDL-CLICK-XDATA", "SHOULD", rel, 1,
                f"HTMLParser could not fully analyze template: {exc}",
            ))
        else:
            for line_no, msg in parser.violations:
                vios.append(Violation("EDL-CLICK-XDATA", "MUST", rel, line_no, msg))

    for m in re.finditer(
        r"<div\b[^>]*class\s*=\s*([\"'])([^\"']*\bmodal-overlay\b[^\"']*)\1[^>]*>",
        text,
        re.I,
    ):
        tag = m.group(0)
        line_no = _line_of(text, m.start())
        if 'role="dialog"' not in tag and "role='dialog'" not in tag:
            vios.append(Violation(
                "EDL-MODAL-DIALOG", "MUST", rel, line_no,
                '.modal-overlay missing role="dialog"',
            ))
        if "aria-modal" not in tag:
            vios.append(Violation(
                "EDL-MODAL-DIALOG", "MUST", rel, line_no,
                ".modal-overlay missing aria-modal",
            ))
        window = text[max(0, m.start() - 800): m.end() + 200]
        if "keydown.escape" not in window:
            vios.append(Violation(
                "EDL-MODAL-ESC", "MUST", rel, line_no,
                "modal overlay (or nearby parent) must bind Escape to dismiss",
            ))

    for m in _HEX_INLINE.finditer(text):
        ctx = text[max(0, m.start() - 40): m.end() + 40]
        if re.search(r"\bbtn\b|<a\b", ctx, re.I):
            vios.append(Violation(
                "EDL-TOKEN-HEX", "SHOULD", rel, _line_of(text, m.start()),
                "prefer CSS variables over inline hex on interactive chrome",
            ))

    if path.name == "base.html":
        if "events-bell" not in text or "events-drawer" not in text:
            vios.append(Violation(
                "EDL-NAV-EVENTS", "MUST", rel, 1,
                "Events bell + drawer pattern missing from base.html",
            ))
        if 'id="toasts"' not in text:
            vios.append(Violation(
                "EDL-TOASTS", "MUST", rel, 1,
                "#toasts toast region missing",
            ))
        if "Decisions" not in text or "/decisions" not in text:
            vios.append(Violation(
                "EDL-NAV-EVENTS", "MUST", rel, 1,
                "Decisions menu link missing from base.html",
            ))
        if re.search(r"btn-danger(?!-outline)", text) and not re.search(r"\.btn-danger\s*\{", text):
            vios.append(Violation(
                "EDL-DANGER-CLASS", "MUST", rel, 1,
                ".btn-danger CSS class must be defined (confirm modal uses it)",
            ))
        for m in _BADGE_FONT.finditer(text):
            if not _badge_font_ok(m.group(1)):
                vios.append(Violation(
                    "EDL-BADGE-MIN", "MUST", rel, _line_of(text, m.start()),
                    f".badge font-size {m.group(1).strip()!r} is below 12px / var(--font-xs)",
                ))
        if not re.search(r"\.filter-bar\s*\{", text):
            vios.append(Violation(
                "EDL-FILTER-CSS", "MUST", rel, 1,
                ".filter-bar CSS missing from base.html",
            ))
        elif not re.search(
            r"\.filter-bar\s+input\s*,\s*\.filter-bar\s+select\s*\{[^}]*width\s*:\s*auto",
            text,
            re.S,
        ):
            vios.append(Violation(
                "EDL-FILTER-CSS", "MUST", rel, 1,
                ".filter-bar must override global input/select width:100% (width: auto)",
            ))

    if path.name == "onboard_results.html":
        if not re.search(r"btn-label\">Dry Run<|\"Dry Run\"|>Dry Run<", text):
            vios.append(Violation(
                "EDL-ONBOARD-ORDER", "MUST", rel, 1,
                "Dry Run control missing from onboard results action bar",
            ))
        # Labels may be inline ("Apply to Cluster") or via Jinja
        # `{% set _deliver_label = "Open PR" if … else "Apply" %}`.
        if not re.search(
            r'Apply to Cluster|Commit & Open PR|'
            r'_deliver_label\s*=\s*["\']Open PR["\']|'
            r'_deliver_label\s*=\s*["\']Apply["\']|'
            r'>Apply<|>Open PR<|'
            r'btn-label\">\{\{\s*_deliver_label',
            text,
        ):
            vios.append(Violation(
                "EDL-ONBOARD-ORDER", "MUST", rel, 1,
                "Apply step label missing (Apply / Open PR / Apply to Cluster / Commit & Open PR)",
            ))
        for line_no, _attrs, inner in _iter_buttons(text):
            if "No dry run yet" in inner:
                vios.append(Violation(
                    "EDL-ONBOARD-ORDER", "MUST", rel, line_no,
                    "dry-run status must be outside the Apply button",
                ))

    # Compact filter toolbar on list/log pages (EDL §6 Filters).
    if path.name in {"decisions.html", "events.html", "ledger.html"}:
        if not re.search(r'''class\s*=\s*["'][^"']*\bfilter-bar\b''', text):
            vios.append(Violation(
                "EDL-FILTER-BAR", "MUST", rel, 1,
                'GET filter form must use class="filter-bar"',
            ))
        for m in re.finditer(
            r'''<form\b[^>]*method\s*=\s*["']get["'][^>]*class\s*=\s*["']([^"']*)["']''',
            text,
            re.I,
        ):
            classes = m.group(1).split()
            if "action-bar" in classes and "filter-bar" not in classes:
                vios.append(Violation(
                    "EDL-FILTER-BAR", "MUST", rel, _line_of(text, m.start()),
                    "GET filter form must not use .action-bar; use .filter-bar",
                ))
        if not re.search(r'''class\s*=\s*["'][^"']*\bfilter-actions\b''', text):
            vios.append(Violation(
                "EDL-FILTER-BAR", "SHOULD", rel, 1,
                "filter form should group Filter/Clear in .filter-actions",
            ))

    return vios


def check_all() -> list[Violation]:
    if not TEMPLATES.is_dir():
        raise SystemExit(f"templates dir not found: {TEMPLATES}")
    all_v: list[Violation] = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        all_v.extend(check_file(path))
    return all_v


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    parser.add_argument("--fail-should", action="store_true", help="Also fail on SHOULD")
    args = parser.parse_args(argv)

    vios = check_all()
    musts = [v for v in vios if v.severity == "MUST"]
    shoulds = [v for v in vios if v.severity == "SHOULD"]

    if args.json:
        print(json.dumps([asdict(v) for v in vios], indent=2))
    else:
        for v in vios:
            stream = sys.stderr if v.severity == "SHOULD" else sys.stdout
            print(f"{v.severity} {v.rule} {v.path}:{v.line}: {v.message}", file=stream)
        print(
            f"EDL check: {len(musts)} MUST, {len(shoulds)} SHOULD "
            f"across {len(list(TEMPLATES.rglob('*.html')))} templates",
            file=sys.stderr,
        )

    if musts:
        return 1
    if args.fail_should and shoulds:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
