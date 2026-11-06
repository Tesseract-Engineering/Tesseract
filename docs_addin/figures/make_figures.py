"""
make_figures.py - the demo design doc's figures, as SVG, rendered to PNG with headless Chrome.

The figures illustrate the design as its author wrote it (the JWKS fetched once, directly from
auth-service; a token from /oauth/token), so they agree with the text Tesseract is asked about.

    python -m docs_addin.figures.make_figures          writes figN.svg and figN.png beside this file
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

INK, MUTED, LINE, SOFT = "#202124", "#5f6368", "#dadce0", "#f8f9fa"
BLUE, BLUE_SOFT = "#1a73e8", "#e8f0fe"
GREEN, GREEN_SOFT = "#188038", "#e6f4ea"
AMBER, AMBER_SOFT = "#b06000", "#fef7e0"
RED, RED_SOFT = "#c5221f", "#fce8e6"
FONT = "Google Sans, Roboto, Helvetica, Arial, sans-serif"


class Svg:
    def __init__(self, w: int, h: int):
        self.w, self.h, self.parts = w, h, []

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, size=14, color=INK, weight=400, anchor="start", italic=False, mono=False):
        family = "Roboto Mono, Menlo, monospace" if mono else FONT
        style = ' font-style="italic"' if italic else ""
        self.add(f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" fill="{color}" '
                 f'font-weight="{weight}" text-anchor="{anchor}"{style}>{escape(s)}</text>')

    def box(self, x, y, w, h, title, lines=(), fill="#fff", stroke=LINE, title_color=INK, dash=False, radius=10):
        d = ' stroke-dasharray="6 5"' if dash else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" '
                 f'stroke-width="1.5"{d}/>')
        self.text(x + w / 2, y + 26, title, 15, title_color, 600, "middle")
        for i, line in enumerate(lines):
            self.text(x + w / 2, y + 48 + i * 19, line, 12.5, MUTED, 400, "middle")

    def arrow(self, x1, y1, x2, y2, label="", color=MUTED, dash=False, label_dy=-8, label_dx=0, both=False):
        d = ' stroke-dasharray="6 5"' if dash else ""
        start = ' marker-start="url(#ah-start)"' if both else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.6"{d} '
                 f'marker-end="url(#ah)"{start}/>')
        if label:
            self.text((x1 + x2) / 2 + label_dx, (y1 + y2) / 2 + label_dy, label, 12, MUTED, 400, "middle")

    def svg(self) -> str:
        defs = (f'<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                f'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker>'
                f'<marker id="ah-start" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="7" markerHeight="7" '
                f'orient="auto"><path d="M10,0 L0,5 L10,10 z" fill="{MUTED}"/></marker></defs>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}"><rect width="100%" height="100%" fill="#fff"/>{defs}'
                + "".join(self.parts) + "</svg>")


def fig1_current() -> Svg:
    s = Svg(1160, 450)
    s.text(30, 36, "Today", 18, INK, 600)
    s.text(30, 58, "auth-service rewrites the namespaced claim back to tenant_id for the billing audience", 13, MUTED)
    s.box(30, 120, 210, 110, "Webhook senders", ["internal services, partners", "bearer token, aud=billing"])
    s.box(370, 120, 180, 110, "api-gateway", ["routes /webhooks/*"])
    s.box(680, 110, 250, 130, "billing-service", ["webhook handler", "reads top-level tenant_id", "and roles from the token"])
    s.box(1000, 120, 140, 110, "Queue", ["billing events"], fill=SOFT)
    s.arrow(240, 175, 370, 175, "POST /webhooks")
    s.arrow(550, 175, 680, 175)
    s.arrow(930, 175, 1000, 175)
    # auth-service, with the shim inside it
    s.add(f'<rect x="370" y="300" width="560" height="120" rx="10" fill="#fff" stroke="{LINE}" stroke-width="1.5"/>')
    s.text(392, 330, "auth-service", 15, INK, 600)
    s.text(392, 354, "issues OIDC ID tokens; tenant_id and", 12.5, MUTED)
    s.text(392, 373, "roles under https://tesseract.example/claims/", 12.5, MUTED)
    s.box(690, 318, 220, 84, "SBX-10 shim", ["namespaced claim to tenant_id", "for the billing audience"], fill=AMBER_SOFT,
          stroke=AMBER, title_color=AMBER, radius=8)
    s.add(f'<path d="M370,360 L135,360 L135,238" fill="none" stroke="{MUTED}" stroke-width="1.6" marker-end="url(#ah)"/>')
    s.text(250, 352, "issues tokens", 12, MUTED, 400, "middle")
    return s


def fig2_proposed() -> Svg:
    s = Svg(1160, 540)
    s.text(30, 36, "Proposed", 18, INK, 600)
    s.text(30, 58, "billing-service verifies tokens itself and reads the claims; the shim is removed", 13, MUTED)
    s.box(30, 130, 190, 100, "Webhook senders", ["bearer ID token"])
    s.box(340, 130, 170, 100, "api-gateway", ["routes /webhooks/*"])
    s.add(f'<rect x="580" y="90" width="400" height="250" rx="12" fill="{BLUE_SOFT}" stroke="{BLUE}" stroke-width="1.5"/>')
    s.text(600, 116, "billing-service", 16, BLUE, 600)
    s.box(605, 135, 170, 88, "Token verifier", ["signature, iss, aud, exp", "JWKS in memory"], radius=8)
    s.box(790, 135, 170, 88, "Webhook handler", ["tenant_id, roles", "from the token"], radius=8)
    s.box(605, 238, 355, 82, "Tenant client", ["plan and account details per webhook", "token from /oauth/token, refreshed hourly"],
          radius=8)
    s.arrow(220, 180, 340, 180, "POST /webhooks")
    s.arrow(510, 180, 605, 180)
    s.arrow(775, 179, 790, 179)
    s.box(1020, 150, 125, 90, "Queue", ["billing events"], fill=SOFT)
    s.arrow(960, 179, 1020, 192)
    s.box(580, 420, 400, 90, "auth-service", ["/.well-known/jwks.json, /oauth/token,", "tenant lookup API (no billing shim)"])
    s.arrow(660, 420, 660, 223, "JWKS once at startup (direct)", dash=True, label_dx=-115, label_dy=45)
    s.arrow(880, 320, 880, 420, "tenant lookup", label_dx=52, label_dy=0, both=True)
    s.add(f'<path d="M580,465 L125,465 L125,238" fill="none" stroke="{MUTED}" stroke-width="1.6" marker-end="url(#ah)"/>')
    s.text(350, 457, "issues ID tokens", 12, MUTED, 400, "middle")
    return s


def fig3_sequence() -> Svg:
    s = Svg(1100, 600)
    s.text(30, 36, "Verifying one webhook", 18, INK, 600)
    lanes = [("Sender", 110), ("api-gateway", 330), ("billing-service", 560), ("auth-service", 800), ("Queue", 1010)]
    for name, x in lanes:
        s.box(x - 80, 60, 160, 44, name, radius=8, fill=SOFT)
        s.add(f'<line x1="{x}" y1="104" x2="{x}" y2="580" stroke="{LINE}" stroke-width="1.5" stroke-dasharray="4 5"/>')
    X = dict(lanes)
    y = 140
    steps = [
        ("billing-service", "auth-service", "GET /.well-known/jwks.json (at startup)", True),
        ("Sender", "api-gateway", "POST /webhooks  (Authorization: Bearer <ID token>)", False),
        ("api-gateway", "billing-service", "forward", False),
        ("billing-service", None, "verify signature with cached keys; check iss, aud, exp", False),
        ("billing-service", None, "read tenant_id and roles", False),
        ("billing-service", "auth-service", "GET /tenants/{id}  (client-credentials token)", False),
        ("auth-service", "billing-service", "200 plan, account", True),
        ("billing-service", "Queue", "enqueue event", False),
        ("billing-service", "Sender", "202 Accepted", True),
    ]
    for i, (a, b, label, ret) in enumerate(steps, 1):
        s.add(f'<circle cx="34" cy="{y - 4}" r="11" fill="{BLUE_SOFT}"/>')
        s.text(34, y + 1, str(i), 12, BLUE, 600, "middle")
        if b is None:
            x = X[a]
            s.add(f'<rect x="{x - 6}" y="{y - 16}" width="12" height="26" fill="{BLUE}" rx="2"/>')
            s.text(x + 16, y + 1, label, 12.5, INK)
        else:
            s.arrow(X[a], y, X[b], y, dash=ret)
            s.text((X[a] + X[b]) / 2, y - 7, label, 12.5, INK, 400, "middle")
        y += 48
    return s


def fig4_timeline() -> Svg:
    s = Svg(1100, 330)
    s.text(30, 36, "Rollout", 18, INK, 600)
    days = list(range(1, 19))
    x0, dx = 250, 46
    for d in days:
        x = x0 + (d - 1) * dx
        s.text(x + dx / 2, 78, f"Dec {d}", 11, MUTED, 400, "middle")
        s.add(f'<line x1="{x}" y1="88" x2="{x}" y2="300" stroke="#f1f3f4" stroke-width="1"/>')
    rows = [("1. Shadow (staging)", 1, 7, BLUE_SOFT, BLUE), ("2. Staging serves", 8, 13, BLUE_SOFT, BLUE),
            ("3. Production switch", 14, 18, GREEN_SOFT, GREEN), ("4. Delete the shim", 14, 14, RED_SOFT, RED)]
    y = 110
    for name, a, b, fill, stroke in rows:
        s.text(30, y + 22, name, 14, INK, 500)
        s.add(f'<rect x="{x0 + (a - 1) * dx + 3}" y="{y}" width="{(b - a + 1) * dx - 6}" height="32" rx="7" '
              f'fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
        y += 48
    s.text(30, 318, "Target dates; the production switch is behind the billing_oidc_verify flag.", 12, MUTED, 400, "start", True)
    return s


FIGURES = {"fig1-current": fig1_current, "fig2-proposed": fig2_proposed, "fig3-sequence": fig3_sequence,
           "fig4-rollout": fig4_timeline}


def render(name: str, svg: Svg) -> None:
    (HERE / f"{name}.svg").write_text(svg.svg(), encoding="utf-8")
    page = HERE / f".{name}.html"
    page.write_text(f'<!doctype html><html><body style="margin:0">{svg.svg()}</body></html>', encoding="utf-8")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    f"--window-size={svg.w},{svg.h}", f"--screenshot={HERE / (name + '.png')}", page.as_uri()],
                   check=True, capture_output=True)
    page.unlink()


if __name__ == "__main__":
    for name, make in FIGURES.items():
        render(name, make())
        print(name, file=sys.stderr)
