"""Generate the README brand hero (light and dark) as plain SVG.

Original illustration in the ConvictionDesk green / warm off-white / gold
palette. System fonts, inline shapes, no embedded media, no external
references. The case card is a schematic with labels and placeholder bars;
it is not a screenshot and shows no data.

    python docs/assets/src/make_brand_hero.py
"""
from pathlib import Path

W, H = 1280, 440
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

THEMES = {
    "light": dict(
        canvas="#FBF8F1", edge="#E7E0D0", card="#FFFFFF", card_edge="#E3DDCF",
        ink="#1E2A27", muted="#566863", wire="#CDC5B4", bar="#ECE6D8",
        green="#27745F", on_green="#FFFFFF", green_soft="#E4EFE9",
        gold="#7D5A1F", gold_soft="#F4EBD6", gold_line="#DCC699",
    ),
    "dark": dict(
        canvas="#111614", edge="#2A3430", card="#1A211E", card_edge="#303B36",
        ink="#EEF2EE", muted="#B2BEB8", wire="#45524C", bar="#2A3430",
        green="#8DC8B5", on_green="#102C23", green_soft="#203A32",
        gold="#E6C481", gold_soft="#2D2819", gold_line="#5E4F30",
    ),
}


def text(x, y, s, size, fill, weight=400, anchor="start", spacing=None):
    ls = f' letter-spacing="{spacing}"' if spacing else ""
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{ls}>{s}</text>')


def rect(x, y, w, h, rx, fill, stroke=None, width=1):
    st = f' stroke="{stroke}" stroke-width="{width}"' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{st}/>'


def hero(t):
    o = []
    add = o.append
    add(rect(0, 0, W, H, 20, t["canvas"]))
    add(rect(0.5, 0.5, W - 1, H - 1, 20, "none", t["edge"]))

    # Left: identity, promise, scope.
    add(rect(64, 72, 52, 52, 12, t["green"]))
    add(text(90, 106, "CD", 20, t["on_green"], 700, "middle"))
    add(text(132, 109, "ConvictionDesk", 30, t["ink"], 650))
    add(text(64, 214, "From holdings", 50, t["ink"], 650))
    add(f'<text x="64" y="272" font-family="{FONT}" font-size="50" font-weight="650" fill="{t["ink"]}">'
        f'to <tspan fill="{t["green"]}">conviction.</tspan></text>')
    add(rect(64, 298, 44, 3, 1.5, t["gold"]))
    add(text(64, 336, "Research the US stocks you own:", 19, t["muted"]))
    add(text(64, 362, "evidence, counterevidence, account consequence.", 19, t["muted"]))
    add(text(64, 400, "Local · open source · no broker connection", 14, t["muted"], 500))

    # Holdings in research order (schematic).
    add(text(620, 106, "YOUR HOLDINGS", 12, t["gold"], 700, spacing="1.6"))
    rows = [("1", "new filing", True), ("2", "not yet researched", False), ("3", "research is stale", False)]
    for i, (n, label, focal) in enumerate(rows):
        y = 120 + i * 46
        if focal:
            add(rect(620, y, 212, 38, 9, t["gold_soft"], t["gold_line"]))
        else:
            add(rect(620, y, 212, 38, 9, t["card"], t["card_edge"]))
        add(text(640, y + 25, n, 15, t["green"], 700, "middle"))
        add(text(658, y + 25, label, 14.5, t["ink"] if focal else t["muted"], 600 if focal else 400))

    # Evidence sources.
    add(text(620, 290, "EVIDENCE", 12, t["gold"], 700, spacing="1.6"))
    x = 620
    for label, w in (("SEC filings", 100), ("XBRL", 60), ("Prices", 68)):
        add(rect(x, 302, w, 30, 15, t["card"], t["card_edge"]))
        add(text(x + w / 2, 322, label, 13.5, t["muted"], 500, "middle"))
        x += w + 8

    # Wires into the evidence cutoff.
    add(f'<path d="M832 139 C 862 139, 860 214, 888 214" fill="none" stroke="{t["green"]}" stroke-width="1.75"/>')
    add(f'<path d="M832 185 C 860 185, 860 226, 888 226" fill="none" stroke="{t["wire"]}" stroke-width="1.5"/>')
    add(f'<path d="M864 317 C 878 317, 874 250, 888 250" fill="none" stroke="{t["wire"]}" stroke-width="1.5"/>')
    add(f'<line x1="888" y1="96" x2="888" y2="392" stroke="{t["green"]}" stroke-width="2" stroke-dasharray="6 6"/>')
    add(text(888, 84, "evidence cutoff", 13, t["green"], 650, "middle"))
    add(f'<path d="M888 232 L 916 232" stroke="{t["green"]}" stroke-width="2"/>')

    # The case file: one task, saved revisions, cited judgment.
    cx, cy, cw, ch = 916, 96, 300, 262
    add(rect(cx, cy, cw, ch, 14, t["card"], t["card_edge"]))
    add(rect(cx + 30, cy - 13, 104, 26, 6, t["gold_soft"], t["gold_line"]))
    add(text(cx + 82, cy + 5, "CASE FILE", 12, t["gold"], 700, "middle", "1.6"))
    add(rect(cx + 184, cy + 22, 46, 24, 12, t["card"], t["card_edge"]))
    add(text(cx + 207, cy + 39, "Fast", 12.5, t["muted"], 600, "middle"))
    add(rect(cx + 236, cy + 22, 48, 24, 12, t["green_soft"]))
    add(text(cx + 260, cy + 39, "Deep", 12.5, t["green"], 650, "middle"))
    add(text(cx + 20, cy + 40, "Judgment", 15, t["ink"], 650))
    add(text(cx + 20, cy + 68, "ADD · HOLD · REDUCE · EXIT", 13.5, t["ink"], 600, spacing="0.3"))
    add(rect(cx + 20, cy + 80, 260, 24, 12, t["gold_soft"], t["gold_line"]))
    add(text(cx + 150, cy + 96.5, "or UNDETERMINED when evidence is thin", 11.5, t["gold"], 650, "middle"))
    for i, (label, bar) in enumerate((("Counterevidence", 108), ("What would change it", 78), ("Next check", 140))):
        y = cy + 140 + i * 30
        add(text(cx + 20, y, label, 13.5, t["muted"]))
        add(rect(cx + cw - 20 - bar, y - 9, bar, 8, 4, t["bar"]))
    for i, ref in enumerate(("[1]", "[2]", "[3]")):
        add(rect(cx + 20 + i * 44, cy + 216, 38, 22, 6, t["green_soft"]))
        add(text(cx + 39 + i * 44, cy + 231, ref, 11.5, t["green"], 650, "middle"))
    add(text(cx + cw - 20, cy + 232, "cited sources", 12, t["muted"], 400, "end"))

    # Account consequence.
    add(f'<line x1="{cx + cw / 2}" y1="{cy + ch}" x2="{cx + cw / 2}" y2="370" stroke="{t["green"]}" stroke-width="1.75"/>')
    add(rect(cx + 8, 370, cw - 16, 34, 17, t["green_soft"]))
    add(text(cx + cw / 2, 392, "What-if → cash · shares · concentration", 13.5, t["green"], 650, "middle"))

    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
            'role="img" aria-labelledby="title desc">\n'
            '<title id="title">ConvictionDesk — From holdings to conviction</title>\n'
            '<desc id="desc">Original brand illustration. Holdings in research order and their evidence '
            'pass an evidence cutoff into one case file: a cited judgment or UNDETERMINED, counterevidence, '
            'what would change it and the next check, saved as Fast and Deep revisions, then a hypothetical '
            'What-if on cash, shares and concentration. A schematic, not a screenshot.</desc>\n')
    return head + "\n".join(o) + "\n</svg>\n"


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1]
    for name, tokens in THEMES.items():
        (out / f"convictiondesk-brand-hero-{name}.svg").write_text(hero(tokens), encoding="utf-8", newline="\n")
        print("wrote", f"convictiondesk-brand-hero-{name}.svg")
