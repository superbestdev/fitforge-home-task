"""Generate a single, properly designed sample service manual.

Unlike `generate_manuals.py`, which produces 300 plain manuals quickly to build
a corpus, this makes ONE document that looks like something a manufacturer
would actually ship: a designed cover, a contents page, vector line-art
illustrations with numbered callouts, styled warning panels, specification and
torque tables, an exploded parts diagram, and running headers and footers.

Its purpose is to give you a realistic file to drag into the console's upload
panel. It is deliberately built for a model that currently has **no** digital
manual, so uploading it closes a real coverage gap and flips that model from
`unbacked` to `backed`.

    python -m seed.generate_sample_manual
    python -m seed.generate_sample_manual --model FF-TT-SUMMIT-950
    python -m seed.generate_sample_manual --out /app/data/sample.pdf
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, NextPageTemplate,
    PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from services.api.app.config import settings
from services.api.app.db import query_one

from .taxonomy import CATEGORY_BY_ID, WARRANTY_PROFILES

log = logging.getLogger(__name__)

# --- palette ---------------------------------------------------------------
NAVY = colors.HexColor("#0f2d52")
NAVY_DARK = colors.HexColor("#08203c")
STEEL = colors.HexColor("#4a6180")
ACCENT = colors.HexColor("#d4761a")
LIGHT = colors.HexColor("#eef2f7")
MID = colors.HexColor("#9aa8bd")
RULE = colors.HexColor("#c6d0dd")
INK = colors.HexColor("#1c2733")
DANGER = colors.HexColor("#a8231f")
DANGER_BG = colors.HexColor("#fbeceb")
CAUTION = colors.HexColor("#8a6100")
CAUTION_BG = colors.HexColor("#fdf6e3")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm

_ss = getSampleStyleSheet()

H1 = ParagraphStyle("H1", parent=_ss["Heading1"], fontName="Helvetica-Bold",
                    fontSize=15, leading=19, textColor=NAVY, spaceBefore=2,
                    spaceAfter=9)
H2 = ParagraphStyle("H2", parent=_ss["Heading2"], fontName="Helvetica-Bold",
                    fontSize=10.5, leading=14, textColor=NAVY, spaceBefore=11,
                    spaceAfter=5)
BODY = ParagraphStyle("Body", parent=_ss["BodyText"], fontName="Helvetica",
                      fontSize=9, leading=13, textColor=INK, alignment=TA_LEFT,
                      spaceAfter=6)
STEP = ParagraphStyle("Step", parent=BODY, leftIndent=13, spaceAfter=4)
LEAD = ParagraphStyle("Lead", parent=BODY, fontSize=9.8, leading=14,
                      textColor=STEEL, spaceAfter=9)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.2, leading=10.5,
                      spaceAfter=0)
CELL_H = ParagraphStyle("CellH", parent=CELL, fontName="Helvetica-Bold",
                        textColor=colors.white)
CAP = ParagraphStyle("Cap", parent=BODY, fontSize=7.6, leading=10,
                     textColor=STEEL, alignment=TA_CENTER, spaceBefore=4)
PART = ParagraphStyle("Part", parent=CELL, fontName="Courier", fontSize=6.7,
                      leading=9.2)


# ===========================================================================
# Vector illustrations
# ===========================================================================

class Illustration(Flowable):
    """Base for the line-art drawings.

    Real service manuals are mostly pictures. Drawing them as vectors keeps the
    PDF born-digital — the text layer stays intact, so this file exercises the
    fast ingestion path rather than OCR.
    """

    def __init__(self, width: float, height: float):
        super().__init__()
        self.width = width
        self.height = height

    def wrap(self, *_):
        return self.width, self.height

    # -- small helpers ----------------------------------------------------
    def _callout(self, c: Canvas, x: float, y: float, label: str,
                 to_x: float, to_y: float) -> None:
        """A numbered bubble with a leader line to the component."""
        c.saveState()
        c.setStrokeColor(STEEL)
        c.setLineWidth(0.5)
        c.setDash(2, 2)
        c.line(x, y, to_x, to_y)
        c.setDash()
        c.setFillColor(colors.white)
        c.setStrokeColor(NAVY)
        c.setLineWidth(0.9)
        c.circle(x, y, 5.4, stroke=1, fill=1)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 6.6)
        c.drawCentredString(x, y - 2.3, label)
        c.restoreState()

    def _dim(self, c: Canvas, x1: float, y: float, x2: float, text: str) -> None:
        """A dimension line with arrow ticks."""
        c.saveState()
        c.setStrokeColor(ACCENT)
        c.setLineWidth(0.6)
        c.line(x1, y, x2, y)
        for x in (x1, x2):
            c.line(x, y - 2.5, x, y + 2.5)
        c.setFillColor(ACCENT)
        c.setFont("Helvetica", 6.4)
        c.drawCentredString((x1 + x2) / 2, y + 4, text)
        c.restoreState()


class TreadmillSideView(Illustration):
    """Side elevation of the machine, with the major assemblies called out."""

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        base_y = h * 0.16

        c.saveState()
        c.setLineJoin(1)
        c.setLineCap(1)

        # --- deck and side rails -----------------------------------------
        c.setFillColor(colors.HexColor("#dfe6ef"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(1.1)
        deck_x, deck_w, deck_h = w * 0.06, w * 0.60, 9
        c.rect(deck_x, base_y + 16, deck_w, deck_h, stroke=1, fill=1)

        # running belt wrapping the rollers
        c.setFillColor(colors.HexColor("#39424e"))
        c.setStrokeColor(NAVY_DARK)
        c.setLineWidth(0.9)
        c.roundRect(deck_x - 3, base_y + 13, deck_w + 6, deck_h + 6, 7,
                    stroke=1, fill=1)

        # rollers, drawn through the belt
        for cx, label in ((deck_x + 4, "rear"), (deck_x + deck_w - 4, "front")):
            c.setFillColor(colors.HexColor("#b9c4d2"))
            c.setStrokeColor(NAVY)
            c.circle(cx, base_y + 19.5, 7.5, stroke=1, fill=1)
            c.setFillColor(STEEL)
            c.circle(cx, base_y + 19.5, 2.2, stroke=0, fill=1)

        # --- motor hood ---------------------------------------------------
        hood_x = deck_x + deck_w - 6
        c.setFillColor(colors.HexColor("#cfd8e4"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(1.1)
        p = c.beginPath()
        p.moveTo(hood_x, base_y + 14)
        p.lineTo(hood_x, base_y + 34)
        p.curveTo(hood_x + 14, base_y + 46, hood_x + 34, base_y + 46,
                  hood_x + 46, base_y + 30)
        p.lineTo(hood_x + 46, base_y + 14)
        p.close()
        c.drawPath(p, stroke=1, fill=1)

        # vent slots on the hood
        c.setStrokeColor(MID)
        c.setLineWidth(0.7)
        for i in range(5):
            vx = hood_x + 12 + i * 6
            c.line(vx, base_y + 20, vx, base_y + 32)

        # --- upright and console -----------------------------------------
        up_x = hood_x + 30
        c.setStrokeColor(NAVY)
        c.setLineWidth(2.4)
        c.line(up_x, base_y + 40, up_x + 16, h * 0.74)

        # console housing
        c.setFillColor(NAVY)
        c.setStrokeColor(NAVY_DARK)
        c.setLineWidth(1)
        c.roundRect(up_x - 4, h * 0.74, 58, 34, 4, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#2f6fb5"))
        c.roundRect(up_x, h * 0.74 + 4, 50, 26, 2, stroke=0, fill=1)

        # handlebar
        c.setStrokeColor(NAVY)
        c.setLineWidth(2.4)
        c.line(up_x - 22, h * 0.70, up_x + 30, h * 0.70)
        c.line(up_x - 22, h * 0.70, up_x - 22, h * 0.70 - 12)

        # --- incline foot and stabiliser ---------------------------------
        c.setStrokeColor(NAVY)
        c.setLineWidth(1.6)
        c.line(deck_x - 2, base_y + 13, deck_x - 2, base_y)
        c.line(deck_x - 14, base_y, deck_x + 12, base_y)
        c.line(hood_x + 22, base_y + 14, hood_x + 22, base_y)
        c.line(hood_x + 8, base_y, hood_x + 40, base_y)

        # ground line
        c.setStrokeColor(RULE)
        c.setLineWidth(0.8)
        c.setDash(4, 3)
        c.line(w * 0.02, base_y - 3, w * 0.96, base_y - 3)
        c.setDash()

        # --- callouts -----------------------------------------------------
        self._callout(c, w * 0.10, h * 0.86, "1", deck_x + 26, base_y + 26)
        self._callout(c, w * 0.30, h * 0.92, "2", up_x + 24, h * 0.755)
        self._callout(c, w * 0.60, h * 0.90, "3", hood_x + 22, base_y + 34)
        self._callout(c, w * 0.86, h * 0.72, "4", hood_x + 42, base_y + 20)
        self._callout(c, w * 0.05, h * 0.45, "5", deck_x + 4, base_y + 19.5)
        self._callout(c, w * 0.88, h * 0.30, "6", hood_x + 22, base_y + 3)

        self._dim(c, deck_x - 3, base_y - 14, deck_x + deck_w + 3, "1 550 mm")

        c.restoreState()


class SerialPlateDiagram(Illustration):
    """Where to find the serial plate — the single most-asked support question."""

    def draw(self):
        c = self.canv
        w, h = self.width, self.height

        c.saveState()
        # underside of the frame, viewed at an angle
        c.setFillColor(colors.HexColor("#e7ecf3"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(1)
        p = c.beginPath()
        p.moveTo(w * 0.10, h * 0.30)
        p.lineTo(w * 0.62, h * 0.30)
        p.lineTo(w * 0.78, h * 0.62)
        p.lineTo(w * 0.26, h * 0.62)
        p.close()
        c.drawPath(p, stroke=1, fill=1)

        # front stabiliser bar
        c.setFillColor(colors.HexColor("#cfd8e4"))
        p = c.beginPath()
        p.moveTo(w * 0.12, h * 0.26)
        p.lineTo(w * 0.60, h * 0.26)
        p.lineTo(w * 0.64, h * 0.34)
        p.lineTo(w * 0.16, h * 0.34)
        p.close()
        c.drawPath(p, stroke=1, fill=1)

        # the plate itself
        plate_x, plate_y = w * 0.36, h * 0.40
        c.setFillColor(colors.white)
        c.setStrokeColor(NAVY_DARK)
        c.setLineWidth(1.1)
        c.rect(plate_x, plate_y, 92, 30, stroke=1, fill=1)
        c.setFillColor(NAVY)
        c.rect(plate_x, plate_y + 21, 92, 9, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 5.6)
        c.drawString(plate_x + 4, plate_y + 23.6, "FITFORGE  ·  SERIAL")
        c.setFillColor(INK)
        c.setFont("Courier-Bold", 8)
        c.drawString(plate_x + 5, plate_y + 11, self.serial)
        c.setFont("Helvetica", 5.6)
        c.setFillColor(STEEL)
        c.drawString(plate_x + 5, plate_y + 4, self.model_id)

        # magnifier ring — sized to frame the plate without colliding with the
        # heading above it or the figure caption below
        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.3)
        c.circle(plate_x + 46, plate_y + 15, 52, stroke=1, fill=0)
        c.setLineWidth(2.6)
        c.line(plate_x + 82, plate_y - 22, plate_x + 104, plate_y - 40)

        c.setFillColor(ACCENT)
        c.setFont("Helvetica-Bold", 7.4)
        c.drawString(w * 0.03, h * 0.90, "SERIAL PLATE LOCATION")
        c.setFillColor(STEEL)
        c.setFont("Helvetica", 7)
        c.drawString(w * 0.03, h * 0.83,
                     "Underside of the frame, on the front stabiliser bar.")
        c.drawString(w * 0.03, h * 0.76,
                     "Fold the deck up and look down the left-hand rail.")
        c.restoreState()

    def __init__(self, width, height, serial: str, model_id: str):
        super().__init__(width, height)
        self.serial = serial
        self.model_id = model_id


class ExplodedDrive(Illustration):
    """Exploded view of the drive assembly, with part callouts.

    The classic service-manual figure: components pulled apart along an axis
    with a dashed centre line and numbered bubbles keyed to the parts table.
    """

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        cy = h * 0.52

        c.saveState()
        # assembly axis
        c.setStrokeColor(MID)
        c.setLineWidth(0.6)
        c.setDash(6, 3)
        c.line(w * 0.05, cy, w * 0.95, cy)
        c.setDash()

        def bolt(x, y, r=4.5):
            c.setFillColor(colors.HexColor("#c3ccd9"))
            c.setStrokeColor(NAVY)
            c.setLineWidth(0.8)
            path = c.beginPath()
            for i in range(6):
                a = math.radians(60 * i)
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
            path.close()
            c.drawPath(path, stroke=1, fill=1)

        # --- motor pulley -------------------------------------------------
        c.setFillColor(colors.HexColor("#b9c4d2"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(1)
        c.circle(w * 0.16, cy, 17, stroke=1, fill=1)
        c.setFillColor(colors.white)
        c.circle(w * 0.16, cy, 6, stroke=1, fill=1)

        # --- drive belt (open loop) ---------------------------------------
        c.setStrokeColor(colors.HexColor("#39424e"))
        c.setLineWidth(3.2)
        c.arc(w * 0.28 - 16, cy - 22, w * 0.28 + 16, cy + 22, 90, 180)
        c.arc(w * 0.36 - 16, cy - 22, w * 0.36 + 16, cy + 22, 270, 180)
        c.line(w * 0.28, cy + 22, w * 0.36, cy + 22)
        c.line(w * 0.28, cy - 22, w * 0.36, cy - 22)

        # --- front roller ---------------------------------------------------
        c.setFillColor(colors.HexColor("#cfd8e4"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(1)
        c.rect(w * 0.46, cy - 13, 76, 26, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#b9c4d2"))
        c.rect(w * 0.46 - 7, cy - 5, 7, 10, stroke=1, fill=1)
        c.rect(w * 0.46 + 76, cy - 5, 7, 10, stroke=1, fill=1)
        # roller shading
        c.setStrokeColor(MID)
        c.setLineWidth(0.5)
        for i in range(6):
            rx = w * 0.46 + 8 + i * 11
            c.line(rx, cy - 12, rx, cy + 12)

        # --- bearing + circlip ---------------------------------------------
        c.setFillColor(colors.white)
        c.setStrokeColor(NAVY)
        c.setLineWidth(1)
        c.circle(w * 0.78, cy, 13, stroke=1, fill=1)
        c.circle(w * 0.78, cy, 6.5, stroke=1, fill=1)
        c.setStrokeColor(STEEL)
        c.setLineWidth(0.7)
        c.circle(w * 0.78, cy, 9.8, stroke=1, fill=0)

        bolt(w * 0.89, cy + 12)
        bolt(w * 0.89, cy - 12)

        # --- callouts -------------------------------------------------------
        self._callout(c, w * 0.14, h * 0.90, "7", w * 0.16, cy + 17)
        self._callout(c, w * 0.32, h * 0.93, "8", w * 0.32, cy + 22)
        self._callout(c, w * 0.55, h * 0.90, "9", w * 0.55, cy + 13)
        self._callout(c, w * 0.78, h * 0.88, "10", w * 0.78, cy + 13)
        self._callout(c, w * 0.93, h * 0.24, "11", w * 0.89, cy - 12)

        c.setFillColor(STEEL)
        c.setFont("Helvetica-Oblique", 6.6)
        c.drawString(w * 0.05, h * 0.06,
                     "Shown exploded along the drive axis. Reassemble in reverse order.")
        c.restoreState()


class DecalLocations(Illustration):
    """Where the safety decals are fixed to the machine.

    Every real service manual carries this figure, and it is genuinely useful:
    a missing decal is a compliance issue and replacements are ordered by
    position reference rather than by part name.

    The drawing is kept to the left half and the key to the right, so the
    leader tags never collide with the legend.
    """

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        art_w = w * 0.54                      # the machine lives left of this
        base_y = h * 0.26

        c.saveState()
        c.setLineJoin(1)

        deck_x, deck_w = art_w * 0.06, art_w * 0.62
        c.setFillColor(colors.HexColor("#e7ecf3"))
        c.setStrokeColor(STEEL)
        c.setLineWidth(1)
        c.rect(deck_x, base_y + 12, deck_w, 7, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#4a5561"))
        c.roundRect(deck_x - 3, base_y + 9, deck_w + 6, 13, 5, stroke=1, fill=1)

        hood_x = deck_x + deck_w - 5
        c.setFillColor(colors.HexColor("#d6dfea"))
        c.setStrokeColor(STEEL)
        p = c.beginPath()
        p.moveTo(hood_x, base_y + 10)
        p.lineTo(hood_x, base_y + 26)
        p.curveTo(hood_x + 10, base_y + 35, hood_x + 26, base_y + 35,
                  hood_x + 34, base_y + 23)
        p.lineTo(hood_x + 34, base_y + 10)
        p.close()
        c.drawPath(p, stroke=1, fill=1)

        up_x = hood_x + 22
        c.setStrokeColor(STEEL)
        c.setLineWidth(2)
        c.line(up_x, base_y + 32, up_x + 10, h * 0.70)
        c.setFillColor(colors.HexColor("#4a5561"))
        c.roundRect(up_x + 1, h * 0.70, 38, 22, 3, stroke=0, fill=1)

        # feet
        c.setStrokeColor(STEEL)
        c.setLineWidth(1.4)
        c.line(deck_x, base_y + 9, deck_x, base_y)
        c.line(deck_x - 9, base_y, deck_x + 9, base_y)
        c.line(hood_x + 16, base_y + 10, hood_x + 16, base_y)
        c.line(hood_x + 6, base_y, hood_x + 26, base_y)

        decals = [
            ("A", deck_x + art_w * 0.16, base_y + 27),
            ("B", hood_x + 17, base_y + 40),
            ("C", up_x + 20, h * 0.70 - 9),
            ("D", deck_x + deck_w * 0.62, base_y + 2),
        ]
        for tag, x, y in decals:
            c.setFillColor(ACCENT)
            c.setStrokeColor(colors.white)
            c.setLineWidth(0.8)
            c.roundRect(x - 6, y - 5, 12, 10, 2, stroke=1, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 6.4)
            c.drawCentredString(x, y - 2.2, tag)

        # --- key, well clear of the artwork ------------------------------
        lx = w * 0.60
        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(lx - 14, h * 0.10, lx - 14, h * 0.92)

        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(lx, h * 0.86, "DECAL")
        c.drawString(lx + 30, h * 0.86, "POSITION")
        c.setStrokeColor(RULE)
        c.line(lx, h * 0.83, w, h * 0.83)

        for i, (tag, where) in enumerate((
            ("A", "Belt warning — deck, front left"),
            ("B", "Mains voltage — motor hood"),
            ("C", "Read the manual — console mast"),
            ("D", "Max user weight — side rail"),
        )):
            y = h * 0.76 - i * 15
            c.setFillColor(ACCENT)
            c.setStrokeColor(colors.white)
            c.setLineWidth(0.7)
            c.roundRect(lx, y - 3, 11, 9.5, 2, stroke=1, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 6.2)
            c.drawCentredString(lx + 5.5, y - 0.4, tag)
            c.setFillColor(STEEL)
            c.setFont("Helvetica", 6.9)
            c.drawString(lx + 30, y, where)

        c.restoreState()


# ===========================================================================
# Smart bike line art
#
# A bike is not a treadmill with a different label: the flywheel, crank and
# resistance mechanism have no treadmill equivalent, and the hazards are
# different too (a coasting flywheel rather than mains voltage under a hood).
# Drawing them properly is what makes a per-category profile worth having.
# ===========================================================================

def _bike_frame(c: Canvas, w: float, h: float, *, heavy: bool = False) -> dict:
    """Shared geometry for every bike drawing. Machine faces left.

    Studio-bike layout, which is what these actually look like: one horizontal
    lower frame between two stabilisers, a front upright carrying the bars and
    console, a rear upright carrying the saddle, the flywheel forward and low
    under its shroud, and the crank amidships. Keeping every member either
    horizontal, vertical or a single deliberate rake is what stops the drawing
    reading as a tangle of crossed lines.

    Returns anchor points so the callout and decal figures can key their leaders
    to real components instead of guessed coordinates.
    """
    lw = 3.4 if heavy else 2.2
    base_y = h * 0.12

    # --- reference geometry ------------------------------------------------
    frame_y = base_y + h * 0.10                      # lower frame member
    fx, fy = w * 0.20, frame_y + h * 0.20            # flywheel centre
    fr = h * 0.165
    fu_x, fu_top = w * 0.36, h * 0.78                # front upright
    ru_x, ru_top = w * 0.74, h * 0.56                # rear upright
    bb_x, bb_y = w * 0.53, frame_y + h * 0.05        # bottom bracket

    c.setLineJoin(1)
    c.setLineCap(1)

    # --- stabilisers -------------------------------------------------------
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 1.3)
    c.line(w * 0.09, base_y, w * 0.31, base_y)
    c.line(w * 0.69, base_y, w * 0.91, base_y)
    c.setLineWidth(lw * 0.9)
    c.line(w * 0.20, base_y, w * 0.20, frame_y)
    c.line(w * 0.80, base_y, w * 0.80, frame_y)

    # --- lower frame -------------------------------------------------------
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 1.5)
    c.line(w * 0.17, frame_y, w * 0.83, frame_y)

    # --- flywheel shroud (drawn behind the wheel) --------------------------
    c.setFillColor(colors.HexColor("#dce4ee"))
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 0.55)
    p = c.beginPath()
    p.moveTo(fx - fr - 7, fy)
    p.curveTo(fx - fr - 7, fy + fr + 16, fx + fr + 7, fy + fr + 16, fx + fr + 7, fy)
    p.lineTo(fx + fr + 7, fy - fr * 0.3)
    p.lineTo(fx - fr - 7, fy - fr * 0.3)
    p.close()
    c.drawPath(p, stroke=1, fill=1)

    # --- flywheel ----------------------------------------------------------
    c.setFillColor(colors.HexColor("#98a4b3"))
    c.setStrokeColor(NAVY_DARK)
    c.setLineWidth(lw * 0.6)
    c.circle(fx, fy, fr, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#c8d1dd"))
    c.circle(fx, fy, fr * 0.40, stroke=1, fill=1)
    c.setFillColor(STEEL)
    c.circle(fx, fy, fr * 0.12, stroke=0, fill=1)
    c.setStrokeColor(colors.HexColor("#6c7684"))
    c.setLineWidth(0.7)
    for i in range(8):
        a = math.radians(45 * i + 11)
        c.line(fx + fr * 0.50 * math.cos(a), fy + fr * 0.50 * math.sin(a),
               fx + fr * 0.88 * math.cos(a), fy + fr * 0.88 * math.sin(a))

    # --- belt guard, flywheel back to the crank ----------------------------
    c.setStrokeColor(MID)
    c.setLineWidth(lw * 0.5)
    c.setDash(3, 2)
    c.line(fx + fr * 0.9, fy - fr * 0.2, bb_x - h * 0.04, bb_y + h * 0.01)
    c.setDash()

    # --- uprights ----------------------------------------------------------
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 1.4)
    c.line(fu_x, frame_y, fu_x + w * 0.025, fu_top)        # front, slight rake back
    c.line(ru_x, frame_y, ru_x - w * 0.015, ru_top)        # rear, slight rake forward

    # --- adjustment collars ------------------------------------------------
    c.setFillColor(ACCENT)
    for cx, cy in ((fu_x + w * 0.012, frame_y + h * 0.30),
                   (ru_x - w * 0.007, frame_y + h * 0.22)):
        c.setStrokeColor(NAVY_DARK)
        c.setLineWidth(lw * 0.35)
        c.roundRect(cx - w * 0.016, cy, w * 0.032, h * 0.035, 2, stroke=1, fill=1)

    # --- resistance housing on the front upright ---------------------------
    c.setFillColor(colors.HexColor("#cfd8e4"))
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 0.5)
    res_x, res_y = fu_x - w * 0.075, frame_y + h * 0.40
    c.roundRect(res_x, res_y, w * 0.085, h * 0.075, 3, stroke=1, fill=1)
    c.setFillColor(ACCENT)
    c.circle(res_x + w * 0.042, res_y + h * 0.105, h * 0.026, stroke=0, fill=1)
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 0.5)
    c.line(res_x + w * 0.042, res_y + h * 0.075,
           res_x + w * 0.042, res_y + h * 0.082)

    # --- crank and pedal ---------------------------------------------------
    c.setFillColor(colors.HexColor("#b9c4d2"))
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 0.55)
    c.circle(bb_x, bb_y, h * 0.042, stroke=1, fill=1)
    c.setLineWidth(lw)
    crank_x, crank_y = bb_x - w * 0.05, bb_y - h * 0.085
    c.line(bb_x, bb_y, crank_x, crank_y)
    c.setFillColor(colors.HexColor("#39424e"))
    c.setStrokeColor(NAVY_DARK)
    c.setLineWidth(lw * 0.4)
    c.roundRect(crank_x - w * 0.032, crank_y - h * 0.016, w * 0.064, h * 0.032, 2,
                stroke=1, fill=1)

    # --- saddle on the rear upright ----------------------------------------
    sx, sy = ru_x - w * 0.015, ru_top
    c.setFillColor(colors.HexColor("#39424e"))
    c.setStrokeColor(NAVY_DARK)
    c.setLineWidth(lw * 0.4)
    p = c.beginPath()
    p.moveTo(sx - w * 0.070, sy + h * 0.010)
    p.curveTo(sx - w * 0.030, sy + h * 0.042, sx + w * 0.020, sy + h * 0.042,
              sx + w * 0.052, sy + h * 0.014)
    p.curveTo(sx + w * 0.018, sy - h * 0.004, sx - w * 0.030, sy - h * 0.004,
              sx - w * 0.070, sy + h * 0.010)
    p.close()
    c.drawPath(p, stroke=1, fill=1)

    # --- handlebars and console on the front upright -----------------------
    bx = fu_x + w * 0.025
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 1.15)
    c.line(bx - w * 0.075, fu_top, bx + w * 0.048, fu_top)
    c.line(bx - w * 0.075, fu_top, bx - w * 0.075, fu_top - h * 0.065)
    c.line(bx + w * 0.048, fu_top, bx + w * 0.048, fu_top - h * 0.045)

    con_w, con_h = w * 0.175, h * 0.135
    con_x, con_y = bx - con_w * 0.42, fu_top + h * 0.022
    c.setStrokeColor(NAVY)
    c.setLineWidth(lw * 0.8)
    c.line(bx, fu_top, bx, con_y)                       # console stem
    c.setFillColor(NAVY)
    c.setStrokeColor(NAVY_DARK)
    c.setLineWidth(lw * 0.42)
    c.roundRect(con_x, con_y, con_w, con_h, 4, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#2f6fb5"))
    c.roundRect(con_x + 4, con_y + 4, con_w - 8, con_h - 8, 2, stroke=0, fill=1)
    if heavy:
        c.setFillColor(colors.HexColor("#8fb4dd"))
        for i in range(3):
            c.rect(con_x + 10, con_y + h * 0.028 + i * (h * 0.026),
                   (con_w - 26) - i * (w * 0.024), h * 0.010, stroke=0, fill=1)

    # ground line
    c.setStrokeColor(RULE)
    c.setLineWidth(0.9)
    c.setDash(4, 3)
    c.line(w * 0.03, base_y - 4, w * 0.97, base_y - 4)
    c.setDash()

    return {
        "base_y": base_y, "fly": (fx, fy, fr), "bb": (bb_x, bb_y),
        "crank": (crank_x, crank_y), "seat": (sx, sy + h * 0.02),
        "bar": (bx, fu_top), "res": (res_x + w * 0.042, res_y + h * 0.038),
        "console": (bx, con_y + con_h * 0.5),
        "res_edge": (res_x, res_y + h * 0.05),
        "front_upright": (fu_x + w * 0.012, frame_y + h * 0.22),
        "rear_upright": (ru_x - w * 0.007, frame_y + h * 0.30),
    }


class SmartBikeSideView(Illustration):
    """Side elevation of the bike, with the major assemblies called out."""

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        c.saveState()
        a = _bike_frame(c, w, h)
        fx, fy, fr = a["fly"]

        # Bubble placement is not decoration: a leader that crosses the part it
        # is not pointing at makes the figure ambiguous. 4 sits high and left so
        # its leader reaches the resistance housing over the top of the
        # flywheel rather than straight through it.
        self._callout(c, w * 0.035, h * 0.26, "1", fx - fr, fy)           # flywheel
        self._callout(c, w * 0.28, h * 0.97, "2", *a["console"])          # console
        self._callout(c, w * 0.62, h * 0.90, "3", a["bar"][0] + w * 0.045,
                      a["bar"][1])                                        # handlebar
        self._callout(c, w * 0.09, h * 0.88, "4", *a["res_edge"])         # resistance
        self._callout(c, w * 0.36, h * 0.07, "5", *a["crank"])            # crank/pedal
        self._callout(c, w * 0.94, h * 0.66, "6", *a["seat"])             # saddle

        self._dim(c, w * 0.09, a["base_y"] - 15, w * 0.91, "1 220 mm")
        c.restoreState()


class BikeDecalLocations(Illustration):
    """Where the safety decals are fixed. Bike hazards are pinch points and a
    coasting flywheel, not mains voltage — so the decals differ from a
    treadmill's even though the figure serves the same purpose."""

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        art_w = w * 0.54

        c.saveState()
        a = _bike_frame(c, art_w, h)
        fx, fy, fr = a["fly"]

        decals = [
            ("A", fx - fr - 4, fy + fr * 0.65),          # shroud, upper left
            ("B", a["bb"][0] + art_w * 0.06, a["bb"][1] - h * 0.02),
            ("C", a["bar"][0] - art_w * 0.10, a["bar"][1] - h * 0.055),
            ("D", *a["rear_upright"]),
        ]
        for tag, x, y in decals:
            c.setFillColor(ACCENT)
            c.setStrokeColor(colors.white)
            c.setLineWidth(0.8)
            c.roundRect(x - 6, y - 5, 12, 10, 2, stroke=1, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 6.4)
            c.drawCentredString(x, y - 2.2, tag)

        lx = w * 0.60
        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(lx - 14, h * 0.10, lx - 14, h * 0.92)

        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(lx, h * 0.86, "DECAL")
        c.drawString(lx + 30, h * 0.86, "POSITION")
        c.setStrokeColor(RULE)
        c.line(lx, h * 0.83, w, h * 0.83)

        for i, (tag, where) in enumerate((
            ("A", "Flywheel pinch warning — shroud"),
            ("B", "Max user weight — frame, near crank"),
            ("C", "Read the manual — handlebar post"),
            ("D", "Minimum insertion — seat post"),
        )):
            y = h * 0.76 - i * 15
            c.setFillColor(ACCENT)
            c.setStrokeColor(colors.white)
            c.setLineWidth(0.7)
            c.roundRect(lx, y - 3, 11, 9.5, 2, stroke=1, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 6.2)
            c.drawCentredString(lx + 5.5, y - 0.4, tag)
            c.setFillColor(STEEL)
            c.setFont("Helvetica", 6.9)
            c.drawString(lx + 30, y, where)

        c.restoreState()


class BikeExplodedDrive(Illustration):
    """Exploded view of the bike drivetrain.

    Laid out left to right in assembly order, and the bubble numbers match the
    parts table beneath it row for row. The flywheel is drawn but not called
    out: it is not a separately orderable part, and numbering something the
    customer cannot buy is how a parts list starts generating wrong orders.
    """

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        cy = h * 0.52

        c.saveState()
        c.setStrokeColor(MID)
        c.setLineWidth(0.6)
        c.setDash(6, 3)
        c.line(w * 0.03, cy, w * 0.97, cy)
        c.setDash()

        def bolt(x, y, r=4.5):
            c.setFillColor(colors.HexColor("#c3ccd9"))
            c.setStrokeColor(NAVY)
            c.setLineWidth(0.8)
            path = c.beginPath()
            for i in range(6):
                ang = math.radians(60 * i)
                px, py = x + r * math.cos(ang), y + r * math.sin(ang)
                path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
            path.close()
            c.drawPath(path, stroke=1, fill=1)

        # --- 7: crank arm --------------------------------------------------
        c.setFillColor(colors.HexColor("#b9c4d2"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(1)
        c.roundRect(w * 0.04, cy - 5, 44, 10, 4, stroke=1, fill=1)
        c.circle(w * 0.04 + 5, cy, 6, stroke=1, fill=1)
        c.circle(w * 0.04 + 39, cy, 4, stroke=1, fill=1)

        # --- 8: pedal ------------------------------------------------------
        px = w * 0.21
        c.setFillColor(colors.HexColor("#8e9aa9"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(0.9)
        c.rect(px - 16, cy - 2, 18, 4, stroke=1, fill=1)      # spindle
        c.setFillColor(colors.HexColor("#39424e"))
        c.setStrokeColor(NAVY_DARK)
        c.roundRect(px, cy - 11, 22, 22, 3, stroke=1, fill=1)  # cage body
        c.setStrokeColor(colors.HexColor("#9aa5b3"))
        c.setLineWidth(0.6)
        for i in range(3):
            c.line(px + 4, cy - 6 + i * 6, px + 18, cy - 6 + i * 6)

        # --- 9: bottom bracket / flywheel bearing --------------------------
        c.setFillColor(colors.white)
        c.setStrokeColor(NAVY)
        c.setLineWidth(1)
        c.circle(w * 0.36, cy, 13, stroke=1, fill=1)
        c.circle(w * 0.36, cy, 6.5, stroke=1, fill=1)
        c.setStrokeColor(STEEL)
        c.setLineWidth(0.7)
        c.circle(w * 0.36, cy, 9.8, stroke=1, fill=0)

        # --- 10: drive belt (open loop) ------------------------------------
        bx0, bx1 = w * 0.47, w * 0.54
        c.setStrokeColor(colors.HexColor("#39424e"))
        c.setLineWidth(3.2)
        c.arc(bx0 - 15, cy - 24, bx0 + 15, cy + 24, 90, 180)
        c.arc(bx1 - 15, cy - 24, bx1 + 15, cy + 24, 270, 180)
        c.line(bx0, cy + 24, bx1, cy + 24)
        c.line(bx0, cy - 24, bx1, cy - 24)
        c.setStrokeColor(MID)
        c.setLineWidth(0.5)
        for i in range(6):
            tx = bx0 + i * ((bx1 - bx0) / 5)
            c.line(tx, cy + 21, tx, cy + 27)

        # --- flywheel: context only, deliberately not numbered -------------
        c.setFillColor(colors.HexColor("#98a4b3"))
        c.setStrokeColor(NAVY_DARK)
        c.setLineWidth(1)
        c.circle(w * 0.70, cy, 27, stroke=1, fill=1)
        c.setFillColor(colors.HexColor("#c8d1dd"))
        c.circle(w * 0.70, cy, 11, stroke=1, fill=1)
        c.setStrokeColor(colors.HexColor("#6c7684"))
        c.setLineWidth(0.6)
        for i in range(8):
            ang = math.radians(45 * i + 11)
            c.line(w * 0.70 + 14 * math.cos(ang), cy + 14 * math.sin(ang),
                   w * 0.70 + 24 * math.cos(ang), cy + 24 * math.sin(ang))
        c.setFillColor(STEEL)
        c.setFont("Helvetica-Oblique", 6.2)
        c.drawCentredString(w * 0.70, cy - 40, "flywheel (not separately supplied)")

        # --- 11: magnet carrier --------------------------------------------
        c.setFillColor(colors.HexColor("#cfd8e4"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(1)
        c.arc(w * 0.84 - 21, cy - 25, w * 0.84 + 21, cy + 25, 250, 220)
        c.setFillColor(ACCENT)
        for dy in (-13, 0, 13):
            c.rect(w * 0.84 - 4, cy + dy - 3, 9, 6, stroke=0, fill=1)

        bolt(w * 0.94, cy + 11)
        bolt(w * 0.94, cy - 11)

        self._callout(c, w * 0.07, h * 0.88, "7", w * 0.07, cy + 5)
        self._callout(c, w * 0.22, h * 0.90, "8", w * 0.22, cy + 11)
        self._callout(c, w * 0.36, h * 0.90, "9", w * 0.36, cy + 13)
        self._callout(c, w * 0.505, h * 0.94, "10", w * 0.505, cy + 24)
        self._callout(c, w * 0.94, h * 0.22, "11", w * 0.84, cy - 19)

        c.setFillColor(STEEL)
        c.setFont("Helvetica-Oblique", 6.6)
        c.drawString(w * 0.03, h * 0.06,
                     "Shown exploded along the drive axis. Reassemble in reverse order.")
        c.restoreState()


def _cover_machine_bike(c: Canvas, w: float, h: float) -> None:
    """Cover portrait of the bike — same drawing, heavier line, no callouts."""
    _bike_frame(c, w, h, heavy=True)


# ===========================================================================
# Styled panels
# ===========================================================================

class Panel(Flowable):
    """A bordered callout box with a coloured spine and a heading."""

    def __init__(self, width, title, lines, *, tone="danger"):
        super().__init__()
        self.width = width
        self.title = title
        self.lines = lines
        self.tone = tone
        self._para = [Paragraph(t, ParagraphStyle(
            "panel", parent=BODY, fontSize=8.4, leading=11.6,
            textColor=DANGER if tone == "danger" else CAUTION, spaceAfter=3,
        )) for t in lines]

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        inner = availWidth - 34
        self._h = 20
        for p in self._para:
            _, ph = p.wrap(inner, availHeight)
            self._h += ph + 3
        self.height = self._h + 6
        return availWidth, self.height

    def draw(self):
        c = self.canv
        edge = DANGER if self.tone == "danger" else CAUTION
        fill = DANGER_BG if self.tone == "danger" else CAUTION_BG

        c.saveState()
        c.setFillColor(fill)
        c.setStrokeColor(edge)
        c.setLineWidth(0.7)
        c.rect(0, 0, self.width, self.height, stroke=1, fill=1)
        c.setFillColor(edge)
        c.rect(0, 0, 3.5, self.height, stroke=0, fill=1)

        # warning triangle
        top = self.height - 12
        c.setFillColor(edge)
        p = c.beginPath()
        p.moveTo(15, top + 1)
        p.lineTo(9, top - 9)
        p.lineTo(21, top - 9)
        p.close()
        c.drawPath(p, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawCentredString(15, top - 7, "!")

        c.setFillColor(edge)
        c.setFont("Helvetica-Bold", 8.6)
        c.drawString(27, top - 7, self.title)

        y = self.height - 24
        for p in self._para:
            _, ph = p.wrap(self.width - 34, self.height)
            p.drawOn(c, 27, y - ph)
            y -= ph + 3
        c.restoreState()


def data_table(rows, widths, *, align_right=None, mono_cols=()):
    """A styled table with a navy header and zebra striping.

    `mono_cols` sets those columns in a narrow monospace face — used for part
    numbers, which are long unbroken tokens that otherwise wrap mid-word.
    """
    body = []
    for r, row in enumerate(rows):
        cells = []
        for col, cell in enumerate(row):
            if r == 0:
                style = CELL_H
            elif col in mono_cols:
                style = PART
            else:
                style = CELL
            cells.append(Paragraph(str(cell), style))
        body.append(cells)
    t = Table(body, colWidths=widths, hAlign="LEFT", repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, NAVY_DARK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]
    if align_right:
        for col in align_right:
            style.append(("ALIGN", (col, 1), (col, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


class Rule(Flowable):
    """A thin accent rule used under section headings."""

    def __init__(self, width=None, colour=ACCENT, thickness=1.6, length=44):
        super().__init__()
        self.width = width or length
        self.height = thickness + 5
        self.colour = colour
        self.thickness = thickness
        self.length = length

    def wrap(self, availWidth, availHeight):
        return availWidth, self.height

    def draw(self):
        self.canv.setFillColor(self.colour)
        self.canv.rect(0, 2, self.length, self.thickness, stroke=0, fill=1)


def heading(text: str) -> list:
    return [Paragraph(text, H1), Rule(), Spacer(1, 3)]


# ===========================================================================
# Page furniture
# ===========================================================================

def _cover_machine(c: Canvas, w: float, h: float) -> None:
    """Large, clean side elevation for the cover. No callouts — this is a
    portrait of the product, not a service figure."""
    base_y = h * 0.14
    c.setLineJoin(1)
    c.setLineCap(1)

    deck_x, deck_w, deck_h = w * 0.05, w * 0.58, 14

    # side rails
    c.setFillColor(colors.HexColor("#e3e9f1"))
    c.setStrokeColor(NAVY)
    c.setLineWidth(1.6)
    c.rect(deck_x, base_y + 22, deck_w, deck_h, stroke=1, fill=1)

    # belt wrapping the rollers
    c.setFillColor(colors.HexColor("#333c47"))
    c.setStrokeColor(NAVY_DARK)
    c.setLineWidth(1.3)
    c.roundRect(deck_x - 5, base_y + 18, deck_w + 10, deck_h + 9, 10,
                stroke=1, fill=1)

    for cx in (deck_x + 6, deck_x + deck_w - 6):
        c.setFillColor(colors.HexColor("#c3ccd9"))
        c.setStrokeColor(NAVY)
        c.setLineWidth(1.2)
        c.circle(cx, base_y + 26.5, 11, stroke=1, fill=1)
        c.setFillColor(STEEL)
        c.circle(cx, base_y + 26.5, 3.2, stroke=0, fill=1)

    # motor hood
    hood_x = deck_x + deck_w - 8
    c.setFillColor(colors.HexColor("#d6dfea"))
    c.setStrokeColor(NAVY)
    c.setLineWidth(1.6)
    p = c.beginPath()
    p.moveTo(hood_x, base_y + 20)
    p.lineTo(hood_x, base_y + 48)
    p.curveTo(hood_x + 20, base_y + 66, hood_x + 50, base_y + 66,
              hood_x + 66, base_y + 42)
    p.lineTo(hood_x + 66, base_y + 20)
    p.close()
    c.drawPath(p, stroke=1, fill=1)

    c.setStrokeColor(MID)
    c.setLineWidth(0.9)
    for i in range(6):
        vx = hood_x + 16 + i * 8
        c.line(vx, base_y + 28, vx, base_y + 45)

    # uprights
    up_x = hood_x + 42
    c.setStrokeColor(NAVY)
    c.setLineWidth(4)
    c.line(up_x, base_y + 56, up_x + 22, h * 0.80)

    # console
    c.setFillColor(NAVY)
    c.setStrokeColor(NAVY_DARK)
    c.setLineWidth(1.2)
    c.roundRect(up_x + 4, h * 0.80, 86, 50, 5, stroke=1, fill=1)
    c.setFillColor(colors.HexColor("#2f6fb5"))
    c.roundRect(up_x + 10, h * 0.80 + 6, 74, 38, 3, stroke=0, fill=1)
    c.setFillColor(colors.HexColor("#8fb4dd"))
    for i in range(3):
        c.rect(up_x + 16, h * 0.80 + 14 + i * 8, 40 - i * 9, 3, stroke=0, fill=1)

    # handlebar
    c.setStrokeColor(NAVY)
    c.setLineWidth(4)
    c.line(up_x - 34, h * 0.74, up_x + 40, h * 0.74)
    c.line(up_x - 34, h * 0.74, up_x - 34, h * 0.74 - 20)

    # feet
    c.setLineWidth(2.4)
    c.line(deck_x - 2, base_y + 18, deck_x - 2, base_y)
    c.line(deck_x - 18, base_y, deck_x + 16, base_y)
    c.line(hood_x + 30, base_y + 20, hood_x + 30, base_y)
    c.line(hood_x + 12, base_y, hood_x + 52, base_y)

    # floor shadow
    c.setStrokeColor(RULE)
    c.setLineWidth(1)
    c.setDash(5, 4)
    c.line(0, base_y - 5, w, base_y - 5)
    c.setDash()


def make_cover_page(model: dict, cat_name: str, cover_art: Callable):
    def draw(c: Canvas, doc):
        c.saveState()
        # full-bleed navy field
        c.setFillColor(NAVY)
        c.rect(0, PAGE_H * 0.52, PAGE_W, PAGE_H * 0.48, stroke=0, fill=1)
        c.setFillColor(NAVY_DARK)
        c.rect(0, PAGE_H * 0.52, PAGE_W, 5, stroke=0, fill=1)
        c.setFillColor(ACCENT)
        c.rect(MARGIN, PAGE_H * 0.52 - 5, 78, 5, stroke=0, fill=1)

        # wordmark
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 27)
        c.drawString(MARGIN, PAGE_H - 46 * mm, "FITFORGE")
        c.setFillColor(colors.HexColor("#8fb4dd"))
        c.setFont("Helvetica", 8.6)
        c.drawString(MARGIN + 2, PAGE_H - 51 * mm,
                     "HOME FITNESS EQUIPMENT  ·  TECHNICAL PUBLICATIONS")

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 21)
        c.drawString(MARGIN, PAGE_H * 0.62, model["name"])
        c.setFillColor(ACCENT)
        c.setFont("Helvetica-Bold", 12.5)
        c.drawString(MARGIN, PAGE_H * 0.585, "SERVICE MANUAL")

        c.setFillColor(colors.HexColor("#8fb4dd"))
        c.setFont("Courier-Bold", 10)
        c.drawString(MARGIN, PAGE_H * 0.555, model["id"])

        # Product line-art in the lower half. A cover that is half empty reads as
        # a draft; this is also the first thing a technician uses to confirm
        # they have picked up the right book.
        c.saveState()
        c.translate(PAGE_W * 0.10, PAGE_H * 0.20)
        cover_art(c, PAGE_W * 0.80, PAGE_H * 0.26)
        c.restoreState()

        c.setFillColor(MID)
        c.setFont("Helvetica-Oblique", 7.2)
        c.drawCentredString(PAGE_W / 2, PAGE_H * 0.175,
                            "Illustration shows the machine in the running position. "
                            "Specifications may vary by market.")

        # footer band on the cover
        c.setFillColor(LIGHT)
        c.rect(0, 0, PAGE_W, 26 * mm, stroke=0, fill=1)
        c.setFillColor(STEEL)
        c.setFont("Helvetica", 7.4)
        c.drawString(MARGIN, 17 * mm,
                     "This manual is intended for use by the equipment owner and by "
                     "FitForge authorised service technicians.")
        c.drawString(MARGIN, 13 * mm,
                     "Read Section 2 in full before carrying out any procedure.")
        c.setFont("Helvetica-Bold", 7.4)
        c.drawString(MARGIN, 8 * mm,
                     f"Document FF-SM-{model['id'][-8:]}  ·  Revision C  ·  "
                     f"Model year {model['model_year']}  ·  {cat_name}")
        c.restoreState()

    return draw


def make_page_furniture(model: dict):
    def draw(c: Canvas, doc):
        c.saveState()
        # header
        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(MARGIN, PAGE_H - MARGIN + 8, PAGE_W - MARGIN, PAGE_H - MARGIN + 8)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 7.6)
        c.drawString(MARGIN, PAGE_H - MARGIN + 12, "FITFORGE")
        c.setFillColor(STEEL)
        c.setFont("Helvetica", 7.6)
        c.drawString(MARGIN + 46, PAGE_H - MARGIN + 12, model["name"])
        c.setFont("Courier", 7.2)
        c.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN + 12, model["id"])

        # footer
        c.setStrokeColor(RULE)
        c.line(MARGIN, MARGIN - 10, PAGE_W - MARGIN, MARGIN - 10)
        c.setFillColor(MID)
        c.setFont("Helvetica", 7)
        c.drawString(MARGIN, MARGIN - 19, "Service Manual · Revision C")
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 8)
        c.drawRightString(PAGE_W - MARGIN, MARGIN - 19, str(c.getPageNumber() - 1))
        c.setFillColor(ACCENT)
        c.rect(PAGE_W / 2 - 11, MARGIN - 20, 22, 1.6, stroke=0, fill=1)
        c.restoreState()

    return draw


# ===========================================================================
# Content
# ===========================================================================

CONTENTS = [
    ("1", "Model identification", "2"),
    ("2", "Safety", "3"),
    ("3", "Specifications", "4"),
    ("4", "Maintenance schedule", "5"),
    ("5", "Troubleshooting", "6"),
    ("6", "Error codes", "8"),
    ("7", "Parts list", "9"),
    ("8", "Warranty", "10"),
]

# ===========================================================================
# Per-category machine profiles
#
# Everything below this line is what actually differs between a treadmill and a
# bike: the drawings, the assemblies they call out, the fasteners, the specs,
# the maintenance schedule and the hazards. The troubleshooting and error-code
# sections need no profile — they are already generated from the shared
# taxonomy, so they are correct for any category.
#
# Adding a third category is mechanical: draw three figures and fill in one
# MachineProfile. Until that is done, `generate` refuses rather than shipping a
# rower manual illustrated with a treadmill.
# ===========================================================================

@dataclass(frozen=True)
class MachineProfile:
    side_view: type                 # Illustration: principal assemblies
    decals: type                    # Illustration: safety decal positions
    exploded: type                  # Illustration: drive assembly
    cover_art: Callable             # (canvas, w, h) -> None
    serial_note: str                # where the plate is, in prose
    callout_key: list               # rows keyed to the side-view bubbles
    drive_parts: list               # rows keyed to the exploded-view bubbles
    torque: list
    specs: Callable                 # (features) -> rows
    safety_panels: tuple             # ((heading, (lines,...), tone), ...)
    safety_bullets: tuple
    maintenance: list
    procedure_title: str
    procedure_steps: tuple
    spec_panel: tuple               # (heading, body) for the caution panel


TREADMILL_CALLOUTS = [
    ["#", "Assembly", "Service note"],
    ["1", "Running belt and deck", "Wear item. Inspect monthly; lubricate every 3 months."],
    ["2", "Console assembly", "Customer replaceable. Disconnect power before removal."],
    ["3", "Motor hood", "Do not remove. Mains voltage inside. Technician only."],
    ["4", "Drive motor", "Technician only."],
    ["5", "Rear roller and tension bolts", "Adjust for belt tension and tracking."],
    ["6", "Incline actuator", "Calibrate from the service menu."],
]

TREADMILL_DRIVE_PARTS = [
    ["#", "Part number", "Description", "Cust. fit"],
    ["7", "{m}-MOTOR", "Drive motor and pulley assembly", "No"],
    ["8", "{m}-BELT", "Running belt, 500 mm x 1 550 mm", "Yes"],
    ["9", "{m}-FRONT-ROLLER", "Front roller, crowned, 60 mm", "Yes"],
    ["10", "{m}-REAR-ROLLER", "Rear roller with sealed bearings", "Yes"],
    ["11", "{m}-DECK", "Reversible running deck", "Yes"],
]

TREADMILL_TORQUE = [
    ["Fastener", "Size", "Torque"],
    ["Rear roller tension bolts", "M8", "Quarter-turn increments only"],
    ["Front roller mounting", "M8", "22 Nm"],
    ["Motor mounting bolts", "M10", "40 Nm"],
    ["Upright to frame", "M10", "45 Nm"],
    ["Console to upright", "M6", "9 Nm"],
    ["Deck mounting", "M8", "24 Nm"],
]

BIKE_CALLOUTS = [
    ["#", "Assembly", "Service note"],
    ["1", "Flywheel and shroud", "Do not run without the shroud fitted. Technician only."],
    ["2", "Console assembly", "Customer replaceable. Unplug the supply before removal."],
    ["3", "Handlebar post and bars", "Check the clamp before every use."],
    ["4", "Resistance actuator", "Calibrate from Settings > Device > Calibrate."],
    ["5", "Crank arm and pedals", "Re-torque at 30 days, then every 6 months."],
    ["6", "Seat post assembly", "Never raise beyond the minimum-insertion mark."],
]

BIKE_DRIVE_PARTS = [
    ["#", "Part number", "Description", "Cust. fit"],
    ["7", "{m}-CRANK-ARM", "Crank arm, 170 mm", "Yes"],
    ["8", "{m}-PEDAL-SET", "Pedal set, dual SPD and cage", "Yes"],
    ["9", "{m}-FLYWHEEL-BEARING", "Bottom bracket and flywheel bearing set", "No"],
    ["10", "{m}-BELT-DRIVE", "Toothed drive belt", "Yes"],
    ["11", "{m}-RESISTANCE-MOTOR", "Magnet carrier and resistance actuator", "No"],
]

BIKE_TORQUE = [
    ["Fastener", "Size", "Torque"],
    ["Pedal into crank arm", "9/16 in", "35 Nm. Right hand thread; left is reversed"],
    ["Crank arm to spindle", "M8", "40 Nm"],
    ["Seat post clamp", "M6", "12 Nm. Never exceed"],
    ["Handlebar post clamp", "M6", "12 Nm"],
    ["Stabiliser to frame", "M8", "24 Nm"],
    ["Console to post", "M5", "6 Nm"],
]


def _treadmill_specs(features: dict) -> list:
    return [
        ["Parameter", "Value"],
        ["Running surface", "500 mm x 1 550 mm"],
        ["Motor", "3.0 CHP continuous duty"],
        ["Speed range", "0.8 - 20.0 km/h"],
        ["Incline range", str(features.get("incline", "0-15%"))],
        ["Console", str(features.get("console", "LCD"))],
        ["Deck finish", str(features.get("deck", "black"))],
        ["Folding", "Yes" if str(features.get("folding")) == "folding" else "No"],
        ["Maximum user weight", "160 kg"],
        ["Unit weight", "118 kg"],
        ["Supply", "220-240 V ~ 50 Hz, dedicated earthed circuit"],
    ]


def _bike_specs(features: dict) -> list:
    return [
        ["Parameter", "Value"],
        ["Flywheel", "18 kg, perimeter weighted"],
        ["Drive", "Toothed belt. No chain maintenance."],
        ["Resistance", str(features.get("resistance", "magnetic"))],
        ["Resistance levels", "100, micro-adjustable"],
        ["Console", str(features.get("console", "10in touch"))],
        ["Pedals", str(features.get("pedals", "dual SPD/cage"))],
        ["Q-factor", "168 mm"],
        ["Adjustment", "Saddle and bars, 4-way each"],
        ["Maximum user weight", "150 kg"],
        ["Unit weight", "58 kg"],
        ["Supply", "External 12 V DC adaptor. No mains voltage inside the frame."],
    ]


PROFILES: dict[str, MachineProfile] = {
    "treadmill": MachineProfile(
        side_view=TreadmillSideView,
        decals=DecalLocations,
        exploded=ExplodedDrive,
        cover_art=_cover_machine,
        serial_note="on the frame beneath the motor hood, at the front of the machine",
        callout_key=TREADMILL_CALLOUTS,
        drive_parts=TREADMILL_DRIVE_PARTS,
        torque=TREADMILL_TORQUE,
        specs=_treadmill_specs,
        safety_panels=(
            ("DANGER — MAINS VOLTAGE", (
                "This equipment contains mains-voltage components. Never remove the "
                "motor hood, the controller cover, or any panel secured with "
                "tamper-resistant screws.",
                "Always unplug the unit at the wall before any inspection or "
                "maintenance procedure described in this manual.",
                "If you smell burning, see smoke or sparks, or the circuit breaker "
                "trips repeatedly, unplug the unit immediately and contact FitForge "
                "service. Do not attempt to diagnose the fault yourself.",
            ), "danger"),
            ("CAUTION — MOVING PARTS", (
                "Keep children and pets away from the equipment at all times, "
                "including when it is not in use. Remove the safety key and store "
                "it out of reach.",
                "Keep hands, feet, loose clothing and hair clear of the belt and "
                "rollers while the unit is powered.",
                "Position the equipment on a level surface with at least 2 m of "
                "clear space behind it and 0.6 m to each side.",
            ), "caution"),
        ),
        safety_bullets=(
            "Always attach the safety key clip before starting a workout. "
            "The belt will not start without it.",
            "Keep children and pets at least two metres from the machine while "
            "it is in use.",
            "Inspect the equipment before every use. Do not use it if any component "
            "is damaged, worn or missing.",
            "Do not exceed the maximum user weight given in Section 3.",
            "Connect only to a dedicated, earthed 220-240 V outlet. Do not use an "
            "extension lead or a multi-way adaptor.",
            "Allow the motor to cool for 10 minutes before servicing after use.",
        ),
        maintenance=[
            ["Interval", "Task"],
            ["Every use", "Wipe the belt and deck. Check the safety key is present and undamaged."],
            ["Weekly", "Vacuum around and beneath the unit. Confirm the belt is running centred."],
            ["Monthly", "Check belt tension. Inspect the power cord along its full length."],
            ["Every 3 months / 250 km", "Lubricate the deck with FitForge silicone lubricant. Do not use oil."],
            ["Every 6 months", "Check all frame fasteners against the torque table in Section 3."],
            ["Every 12 months", "Inspect the drive belt for glazing or cracking. Inspect roller bearings."],
        ],
        procedure_title="Lubrication procedure",
        procedure_steps=(
            "Unplug the unit at the wall and remove the safety key.",
            "Lift one edge of the belt at the centre of the deck.",
            "Apply 15 ml of silicone lubricant along the centre line of the deck, "
            "reaching about 300 mm forward and back from the middle.",
            "Repeat on the opposite side.",
            "Refit the safety key, run the belt at 4 km/h for three minutes without "
            "anyone on it, then wipe away any excess.",
        ),
        spec_panel=(
            "CAUTION - BELT TENSION",
            "Rear roller tension bolts are adjusted in quarter-turn increments and "
            "are not torque-specified. Never exceed one full turn in total from the "
            "factory setting; over-tensioning destroys the roller bearings and "
            "voids coverage on the roller.",
        ),
    ),
    "bike": MachineProfile(
        side_view=SmartBikeSideView,
        decals=BikeDecalLocations,
        exploded=BikeExplodedDrive,
        cover_art=_cover_machine_bike,
        serial_note="on the frame just behind the crank, on the non-drive side",
        callout_key=BIKE_CALLOUTS,
        drive_parts=BIKE_DRIVE_PARTS,
        torque=BIKE_TORQUE,
        specs=_bike_specs,
        safety_panels=(
            ("DANGER — FIXED-GEAR FLYWHEEL", (
                "The flywheel is directly coupled to the pedals and does not "
                "freewheel. It carries enough momentum to keep the cranks turning "
                "hard after you stop pedalling.",
                "Bring the pedals to a complete stop using the emergency brake "
                "lever before dismounting or reaching near the flywheel.",
                "Never remove the flywheel shroud, and never operate the machine "
                "with it removed or damaged.",
            ), "danger"),
            ("CAUTION — CLAMPS AND PINCH POINTS", (
                "Check both the seat post and handlebar post clamps before every "
                "use. A clamp that slips under load will drop the rider onto the "
                "frame.",
                "Keep children and pets away from the equipment at all times. The "
                "gap between the crank and the frame is a pinch point even when "
                "the machine is unpowered.",
                "Position the machine on a level surface with at least 0.6 m of "
                "clear space on every side.",
            ), "caution"),
        ),
        safety_bullets=(
            "The flywheel is fixed-gear and does not freewheel. It will keep the "
            "pedals turning after you stop. Never dismount before the pedals stop.",
            "Never run the machine with the flywheel shroud removed.",
            "Check the seat post and handlebar post clamps before every use. Never "
            "raise either beyond its minimum-insertion mark.",
            "Keep children and pets at least two metres from the machine while "
            "it is in use.",
            "Do not exceed the maximum user weight given in Section 3.",
            "Use only the supplied 12 V adaptor. There is no mains voltage inside "
            "the frame, and no user-serviceable part in the adaptor.",
        ),
        maintenance=[
            ["Interval", "Task"],
            ["Every use", "Wipe sweat from the frame, bars and post. Sweat corrodes the clamps."],
            ["Weekly", "Check the pedals are tight in the crank arms. Vacuum beneath the unit."],
            ["Monthly", "Check seat and handlebar clamp torque. Inspect the adaptor lead."],
            ["Every 3 months", "Check drive belt tension. Inspect for fraying along the tooth line."],
            ["Every 6 months", "Re-torque all frame fasteners against the table in Section 3."],
            ["Every 12 months", "Inspect flywheel bearings for play. Calibrate the resistance actuator."],
        ],
        procedure_title="Pedal and crank torque check",
        procedure_steps=(
            "Unplug the adaptor and wait for the flywheel to stop completely.",
            "Hold the crank arm and try to rock the pedal by hand. Any movement "
            "means the pedal is loose.",
            "The left pedal has a REVERSED thread. Turn it anti-clockwise to "
            "tighten and clockwise to remove.",
            "Torque both pedals to 35 Nm.",
            "Check the crank arm on the spindle the same way, and torque to 40 Nm "
            "if there is any movement.",
        ),
        spec_panel=(
            "CAUTION - SEAT POST INSERTION",
            "The seat post carries a minimum-insertion mark. Raising the post beyond "
            "it puts the whole rider load on the top of the frame tube rather than on "
            "the sleeve. It will crack the frame, and it voids coverage on both the "
            "frame and the post.",
        ),
    ),
}


def build_story(model: dict, cat, features: dict, serial_example: str,
                prof: MachineProfile) -> list:
    content_w = PAGE_W - 2 * MARGIN
    m = model["id"]
    story: list = []

    # ---------- page 2: contents + identification -------------------------
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())

    story += heading("CONTENTS")
    story.append(data_table(
        [["Section", "Title", "Page"]] + [[a, b, cpage] for a, b, cpage in CONTENTS],
        [58, content_w - 118, 60], align_right=[2],
    ))
    story.append(Spacer(1, 14))

    story += heading("SECTION 1 — MODEL IDENTIFICATION")
    story.append(Paragraph(
        "Confirm the model before ordering parts or following any procedure in "
        "this manual. Procedures and part numbers differ between models in the "
        "same range, and fitting the wrong part will not resolve the fault.",
        LEAD))

    story.append(SerialPlateDiagram(content_w, 118, serial_example, m))
    story.append(Paragraph("Figure 1 — Serial plate location", CAP))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"The plate is fixed {prof.serial_note}.", BODY))

    story.append(Paragraph(
        f"Serial numbers for this model begin with <b>{model['serial_prefix']}</b>, "
        f"followed by a two-digit year and a five-digit sequence.", BODY))

    feature_rows = [["Characteristic", "This model"]] + [
        [k.replace("_", " ").title(), str(v)] for k, v in features.items()
    ]
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        "If the plate is missing or unreadable, the following characteristics "
        "distinguish this model from others in the range:", BODY))
    story.append(data_table(feature_rows, [content_w * 0.38, content_w * 0.62]))

    # ---------- page 3: safety --------------------------------------------
    story.append(PageBreak())
    story += heading("SECTION 2 — SAFETY")
    story.append(Paragraph(
        "Read this section completely before operating or servicing the "
        "equipment. The procedures in this manual assume the machine is "
        "unplugged unless a step explicitly states otherwise.", LEAD))

    for title, lines, tone in prof.safety_panels:
        story.append(Panel(content_w, title, list(lines), tone=tone))
        story.append(Spacer(1, 8))
    story.append(Spacer(1, 3))

    story.append(Paragraph("General requirements", H2))
    for line in prof.safety_bullets:
        story.append(Paragraph(f"•&nbsp;&nbsp;{line}", STEP))

    story.append(Spacer(1, 12))
    story.append(Paragraph("Safety decal locations", H2))
    story.append(Paragraph(
        "Replace any decal that is missing, torn or illegible before returning "
        "the machine to service. Order replacements by position reference.",
        BODY))
    story.append(prof.decals(content_w, 132))
    story.append(Paragraph("Figure 2 — Safety decal locations", CAP))

    # ---------- page 4: specifications ------------------------------------
    story.append(PageBreak())
    story += heading("SECTION 3 — SPECIFICATIONS")

    story.append(prof.side_view(content_w, 175))
    story.append(Paragraph("Figure 3 — Principal assemblies", CAP))
    story.append(Spacer(1, 7))
    story.append(data_table(prof.callout_key,
                            [32, content_w * 0.30, content_w * 0.62]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Technical data", H2))
    specs = prof.specs(features)
    story.append(data_table(specs, [content_w * 0.42, content_w * 0.58]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Torque specifications", H2))
    story.append(data_table(prof.torque,
                            [content_w * 0.46, content_w * 0.18, content_w * 0.36],
                            align_right=[2]))
    story.append(Spacer(1, 6))
    story.append(Panel(content_w, prof.spec_panel[0], [prof.spec_panel[1]],
                       tone="caution"))

    # ---------- page 5: maintenance ---------------------------------------
    story.append(PageBreak())
    story += heading("SECTION 4 — MAINTENANCE SCHEDULE")
    story.append(Paragraph(
        "The majority of service calls are caused by missed maintenance. "
        "Following this schedule prevents most of the faults described in "
        "Section 5.", LEAD))
    story.append(data_table(prof.maintenance,
                            [content_w * 0.28, content_w * 0.72]))
    story.append(Spacer(1, 10))
    story.append(Paragraph(prof.procedure_title, H2))
    for i, line in enumerate(prof.procedure_steps, start=1):
        story.append(Paragraph(f"{i}.&nbsp;&nbsp;{line}", STEP))

    # ---------- pages 6-7: troubleshooting --------------------------------
    story.append(PageBreak())
    story += heading("SECTION 5 — TROUBLESHOOTING")
    story.append(Paragraph(
        "Work through the checks for a symptom in the order given. They are "
        "ordered cheapest and safest first, and a later check often assumes an "
        "earlier one has been completed. Do not skip ahead.", LEAD))

    for idx, fault in enumerate(cat.faults):
        block: list = [Paragraph(f"Symptom: {fault.symptom}", H2)]
        if fault.aliases:
            block.append(Paragraph(
                f"<i>Also described as: {', '.join(fault.aliases)}.</i>", BODY))
        if fault.safety_note:
            block.append(Spacer(1, 3))
            block.append(Panel(content_w, "WARNING", [fault.safety_note],
                               tone="danger"))
            block.append(Spacer(1, 5))
        for n, step in enumerate(fault.steps, start=1):
            block.append(Paragraph(f"{n}.&nbsp;&nbsp;{step}", STEP))
        if fault.likely_part_slugs:
            block.append(Paragraph(
                "If the checks above do not resolve the symptom, the following "
                "parts are the likely cause, most likely first: "
                + ", ".join(s.replace("-", " ") for s in fault.likely_part_slugs)
                + ".", BODY))
        block.append(Spacer(1, 7))
        story.append(KeepTogether(block))
        if idx == 2:
            story.append(PageBreak())

    # ---------- page 8: error codes ---------------------------------------
    story.append(PageBreak())
    story += heading("SECTION 6 — ERROR CODES")
    story.append(Paragraph(
        "Codes are shown on the console. Record the code before power cycling — "
        "it is not retained across a restart.", LEAD))
    story.append(data_table(
        [["Code", "Meaning", "First actions"]]
        + [[ec.code, f"{ec.title}. {ec.meaning}", ec.first_actions]
           for ec in cat.error_codes],
        [42, content_w * 0.40, content_w * 0.52],
    ))

    # ---------- page 9: parts ---------------------------------------------
    story.append(PageBreak())
    story += heading("SECTION 7 — PARTS LIST")
    story.append(prof.exploded(content_w, 150))
    story.append(Paragraph("Figure 4 — Drive assembly, exploded view", CAP))
    story.append(Spacer(1, 8))
    story.append(data_table(
        [prof.drive_parts[0]] + [[a, b.format(m=m), cdesc, d]
                                 for a, b, cdesc, d in prof.drive_parts[1:]],
        [26, content_w * 0.34, content_w * 0.44, 52], mono_cols=(1,),
    ))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Full parts list", H2))
    story.append(Paragraph(
        "Quote the complete part number when ordering. Parts marked technician "
        "only must not be fitted by the customer; doing so ends the remaining "
        "coverage on the machine.", BODY))
    story.append(data_table(
        [["Part number", "Description", "Class", "Cust. fit"]]
        + [[f"{m}-{pt.slug.upper()}", pt.name, pt.part_class.title(),
             "Yes" if pt.customer_replaceable else "No"]
            for pt in cat.parts],
        [content_w * 0.34, content_w * 0.36, content_w * 0.16, content_w * 0.14],
        mono_cols=(0,),
    ))

    # ---------- page 10: warranty ------------------------------------------
    story.append(PageBreak())
    story += heading("SECTION 8 — WARRANTY")
    p = WARRANTY_PROFILES[model["category_id"]]
    story.append(data_table([
        ["Component group", "Coverage from date of purchase"],
        ["Frame", f"{p['frame']} months"],
        ["Mechanical parts", f"{p['parts']} months"],
        ["Electronics", f"{p['electronics']} months"],
        ["Labour", f"{p['labor']} months"],
        ["Wear and consumable items", "90 days, manufacturing defects only"],
    ], [content_w * 0.42, content_w * 0.58]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Coverage begins on the date of purchase recorded against the serial "
        "number. Commercial or institutional use voids parts and labour "
        "coverage under the residential warranty. Wear items — belts, decks, "
        "rollers, straps, pads and lubricant — are covered for manufacturing "
        "defects for 90 days from purchase and are not covered thereafter.",
        BODY))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "To make a claim, contact FitForge support with the serial number and "
        "the order number. Retain proof of purchase; coverage attaches to the "
        "purchase record rather than to the owner.", BODY))

    return story


# ===========================================================================
# Entry point
# ===========================================================================

def generate(model_id: str | None, out_path: Path) -> Path:
    model = None
    if model_id:
        model = query_one(
            "SELECT id, name, category_id, model_year, serial_prefix, features "
            "FROM models WHERE id = %s", (model_id,))
        if model is None:
            raise SystemExit(f"no such model: {model_id}")
    else:
        # Prefer a model with no digital manual, so uploading the result
        # actually closes a coverage gap in the console's backfill queue. Among
        # those, prefer the one with the most support traffic — that is the one
        # sitting at the top of the backfill queue, so the demo lines up.
        model = query_one("""
            SELECT m.id, m.name, m.category_id, m.model_year, m.serial_prefix,
                   m.features,
                   (SELECT count(*) FROM issue_threads t
                     WHERE t.model_id = m.id) AS traffic
              FROM models m
              JOIN coverage_registry c ON c.model_id = m.id
             WHERE c.status = 'unbacked'
               AND m.category_id = ANY(%(cats)s)
             ORDER BY traffic DESC, m.id
             LIMIT 1
        """, {"cats": list(PROFILES)}) or query_one(
            "SELECT id, name, category_id, model_year, serial_prefix, features "
            "FROM models WHERE category_id = ANY(%(cats)s) LIMIT 1",
            {"cats": list(PROFILES)})

    if model is None:
        raise SystemExit("no models found — run `python -m seed.generate_catalog`")

    model = dict(model)

    # Every figure in this document — side elevation, decal positions, exploded
    # drive assembly — is drawn per category. Falling back to another category's
    # art would produce a manual whose text is right and whose pictures show the
    # wrong machine, which is worse than having no pictures at all. So refuse.
    prof = PROFILES.get(model["category_id"])
    if prof is None:
        raise SystemExit(
            f"{model['id']} is a {model['category_id']}, and this generator only "
            f"has illustrations for: {', '.join(sorted(PROFILES))}.\n"
            f"Draw a side view, a decal figure and an exploded view for "
            f"{model['category_id']}, then add a MachineProfile for it."
        )

    features = model.get("features") or {}
    if isinstance(features, str):
        features = json.loads(features)

    cat = CATEGORY_BY_ID[model["category_id"]]
    serial_example = f"{model['serial_prefix']}{str(model['model_year'])[2:]}41827"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=f"{model['name']} Service Manual",
        author="FitForge Technical Publications",
        subject=f"Service manual for {model['id']}",
    )
    frame = Frame(MARGIN, MARGIN, PAGE_W - 2 * MARGIN, PAGE_H - 2 * MARGIN,
                  id="main", showBoundary=0)
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[frame],
                     onPage=make_cover_page(model, cat.name, prof.cover_art)),
        PageTemplate(id="body", frames=[frame],
                     onPage=make_page_furniture(model)),
    ])

    doc.build(build_story(model, cat, features, serial_example, prof))
    return out_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Generate one realistic sample service manual for upload testing")
    parser.add_argument("--model",
                        help="model id; defaults to the unbacked model with the "
                             "most support traffic")
    parser.add_argument("--out", help="output path")
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path(settings.manuals_dir).parent / "sample"
    if out.suffix.lower() != ".pdf":
        out = out / "FitForge_Sample_Service_Manual.pdf"

    path = generate(args.model, out)
    size_kb = path.stat().st_size / 1024

    log.info("-" * 66)
    log.info("wrote %s (%.0f KB)", path, size_kb)
    log.info("")
    log.info("Upload it in the agent console -> Manuals -> drop zone.")
    log.info("The model is printed inside the document, so leave the target")
    log.info("blank and it will be detected automatically.")
    log.info("-" * 66)


if __name__ == "__main__":
    main()
