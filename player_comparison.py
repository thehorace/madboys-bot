"""Shared values and a Discord-sized graphic for recorded player comparisons."""
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageDraw

from fonts import font


def _number(value, spec=",.0f", suffix=""):
    return "—" if value is None else f"{value:{spec}}{suffix}"


def _values(player):
    played = player["games"]
    record = " / ".join(str(player[key]) for key in ("wins", "draws", "losses"))
    goals, assists = player["goals"], player["assists"]
    per_match = (goals + assists) / played if goals is not None and assists is not None else None
    return (
        str(played),
        record,
        _number(player["wins"] / played * 100, ".1f", "%"),
        _number(player["avg_rating"], ".2f"),
        _number(goals),
        _number(assists),
        _number(per_match, ".2f"),
        _number(player["pass_accuracy_pct"], ".1f", "%"),
        _number(player["motm"]),
    )


LABELS = ("Matches", "W / D / L", "Win rate", "Avg. rating", "Goals", "Assists",
          "G+A per match", "Pass accuracy", "EA MOTM")
ROLES = {"goalkeeper": "GK", "defender": "DEF", "midfielder": "MID", "forward": "FWD", "unknown": "Unknown"}


def _roles(player):
    return "  ·  ".join(f"{ROLES.get(role.lower(), role.upper())} {count}"
                       for role, count in list(player["positions_played"].items())[:4])


@dataclass(frozen=True)
class PlayerComparison:
    club: str
    days: float
    left: dict
    right: dict

    @property
    def title(self):
        return f"{self.left['player']} vs {self.right['player']}"

    @property
    def period(self):
        return f"Past {self.days:g} {'day' if self.days == 1 else 'days'}" if self.days else "All recorded matches"

    def rows(self):
        return list(zip(LABELS, _values(self.left), _values(self.right)))

    def summary(self):
        lines = [self.title, f"{self.club} · {self.period}"]
        lines.extend(f"{label}: {left}  |  {right}" for label, left, right in self.rows())
        return "\n".join(lines)


def _fit(draw, xy, text, width, size, colour, *, anchor="lm"):
    text = " ".join(text.split())
    face = font(size, True)
    if draw.textlength(text, font=face) > width:
        face = font(max(16, size - 8), True)
        while text and draw.textlength(text + "…", font=face) > width:
            text = text[:-1]
        text += "…"
    draw.text(xy, text, font=face, fill=colour, anchor=anchor)


def render_comparison(card: PlayerComparison) -> BytesIO:
    width, height = 900, 690
    img = Image.new("RGB", (width, height), "#0A1422")
    d = ImageDraw.Draw(img)
    ink, muted = "#F5F8FC", "#AABDD0"
    teal, amber = "#5BE6C4", "#FFC980"
    d.rectangle((0, 0, 450, 7), fill=teal)
    d.rectangle((450, 0, 900, 7), fill=amber)
    d.text((34, 29), "PLAYER COMPARISON", font=font(38, True), fill=ink)
    _fit(d, (36, 101), f"{card.club}  ·  {card.period}", 830, 23, muted)

    for x, player, colour, panel in (
        (34, card.left, teal, "#102D34"), (466, card.right, amber, "#302922")
    ):
        d.rounded_rectangle((x, 130, x + 400, 224), radius=16, fill=panel)
        d.rounded_rectangle((x + 12, 148, x + 17, 206), radius=2, fill=colour)
        _fit(d, (x + 30, 161), player["player"], 350, 35, colour)
        _fit(d, (x + 30, 199), _roles(player), 350, 19, muted)

    for i, (label, left, right) in enumerate(card.rows()):
        y = 246 + 44 * i
        fill = "#102033" if i % 2 == 0 else "#0C1929"
        d.rounded_rectangle((34, y, 866, y + 41), radius=8, fill=fill)
        d.text((218, y + 21), left, font=font(28, True), fill=ink, anchor="mm")
        d.text((450, y + 21), label, font=font(20, True), fill=muted, anchor="mm")
        d.text((682, y + 21), right, font=font(28, True), fill=ink, anchor="mm")

    d.text((36, 667), "EA Pro Clubs · Recorded match history", font=font(18), fill=muted, anchor="lm")
    png = BytesIO()
    img.save(png, format="PNG")
    png.seek(0)
    return png
