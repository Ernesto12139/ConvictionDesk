"""Original evidence-to-decision flow diagram for the v0.10.0 README (light and dark)."""
from pathlib import Path
from xml.sax.saxutils import escape

STAGES = [
    ("Filing", "What the company filed",
     ["Accession, form, items and", "acceptance time, checked", "against SEC's own clock"]),
    ("Thesis", "Why you own it",
     ["Only a version stored strictly", "before the filing is used;", "none means none"]),
    ("Research", "Does this change it?",
     ["Every claim cites evidence", "in the task; thin evidence", "stays UNDETERMINED"]),
    ("Revision", "Continue, don't overwrite",
     ["Fast and Deep are rounds", "of one task; saved rounds", "are immutable"]),
    ("What-if", "What would it change?",
     ["Shares, cash, NAV, weight", "in Python Decimal; missing", "prices stay unknown"]),
]

THEMES = {
    "light": dict(bg="#F5F7F6", card="#FFFFFF", ink="#1E2A26", slate="#56665F", line="#C9D3CE", green="#27745F"),
    "dark": dict(bg="#101814", card="#18221E", ink="#E4ECE8", slate="#9DB0A8", line="#2C3A34", green="#62B89B"),
}

W, H = 1600, 380
CARD_W, GAP, X0, Y0, CARD_H = 272, 40, 46, 70, 230
FONT = "Segoe UI, -apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif"


def svg(theme):
    t = THEMES[theme]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
           f'role="img" aria-label="Filing, thesis, research, revision and what-if: the five steps of one ConvictionDesk decision, '
           f'each with the rule the code enforces.">',
           f'<rect width="{W}" height="{H}" rx="16" fill="{t["bg"]}"/>',
           f'<text x="{X0}" y="44" font-family="{FONT}" font-size="17" fill="{t["slate"]}">One decision, five steps. '
           f'Under each step: the rule the code enforces.</text>']
    for i, (name, question, rules) in enumerate(STAGES):
        x = X0 + i * (CARD_W + GAP)
        last = i == len(STAGES) - 1
        out.append(f'<rect x="{x}" y="{Y0}" width="{CARD_W}" height="{CARD_H}" rx="10" fill="{t["card"]}" '
                   f'stroke="{t["green"] if last else t["line"]}" stroke-width="{2 if last else 1.5}"/>')
        out.append(f'<circle cx="{x + 34}" cy="{Y0 + 40}" r="15" fill="{t["green"]}"/>')
        out.append(f'<text x="{x + 34}" y="{Y0 + 46}" text-anchor="middle" font-family="{FONT}" font-size="16" '
                   f'font-weight="700" fill="{t["card"]}">{i + 1}</text>')
        out.append(f'<text x="{x + 60}" y="{Y0 + 47}" font-family="{FONT}" font-size="23" font-weight="700" '
                   f'fill="{t["ink"]}">{escape(name)}</text>')
        out.append(f'<text x="{x + 22}" y="{Y0 + 92}" font-family="{FONT}" font-size="17" font-style="italic" '
                   f'fill="{t["ink"]}">{escape(question)}</text>')
        out.append(f'<line x1="{x + 22}" y1="{Y0 + 112}" x2="{x + CARD_W - 22}" y2="{Y0 + 112}" stroke="{t["line"]}"/>')
        for j, line in enumerate(rules):
            out.append(f'<text x="{x + 22}" y="{Y0 + 146 + j * 26}" font-family="{FONT}" font-size="16" '
                       f'fill="{t["slate"]}">{escape(line)}</text>')
        if not last:
            ax = x + CARD_W + 6
            ay = Y0 + CARD_H / 2
            out.append(f'<path d="M{ax} {ay} h{GAP - 14}" stroke="{t["green"]}" stroke-width="2"/>')
            out.append(f'<path d="M{ax + GAP - 18} {ay - 6} l8 6 l-8 6" fill="none" stroke="{t["green"]}" stroke-width="2"/>')
    out.append(f'<text x="{X0}" y="{H - 30}" font-family="{FONT}" font-size="16" fill="{t["slate"]}">'
               f'A follow-up continues the same task as a new round, so the reasoning stays on one record.</text>')
    out.append('</svg>')
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    here = Path(__file__).parent
    for theme in THEMES:
        (here / f"convictiondesk-flow-v010-{theme}.svg").write_bytes(svg(theme).encode("utf-8"))
