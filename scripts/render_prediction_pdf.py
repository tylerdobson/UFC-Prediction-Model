#!/usr/bin/env python3
"""Render canonical public prediction JSON as a polished, standalone PDF.

Optional dependency: ``pip install reportlab``. The model and normal report
exports do not import this script or require ReportLab. Run from the project
root with ``PYTHONPATH=src python scripts/render_prediction_pdf.py report.json
--output output/pdf/fight-forecast.pdf``. Existing outputs are never replaced.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import date, datetime
from pathlib import Path

from ufc_odds_model.prediction_report import (
    validate_report, _factor_texts, _favorite, _coverage_text, measurement_rows, measurement_note,
)

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph
except ImportError as exc:
    raise SystemExit("PDF rendering requires the optional package reportlab. Install it with: pip install reportlab") from exc


PAPER = colors.HexColor("#FCFBF8")
INK = colors.HexColor("#171A1C")
RED = colors.HexColor("#B52D2C")
MUTED = colors.HexColor("#686B6B")
LINE = colors.HexColor("#D8D9D5")
WASH = colors.HexColor("#F0F0EC")
SOFT_RED = colors.HexColor("#F6EAE5")
WIDTH, HEIGHT, MARGIN = 612, 792, 42
CONTENT = WIDTH - 2 * MARGIN
BOTTOM = 66


def register_fonts():
    fonts = {"body": "Helvetica", "bold": "Helvetica-Bold", "head": "Helvetica-Bold", "serif": "Times-Bold"}
    locations = {
        "body": ("ReportArial", "/System/Library/Fonts/Supplemental/Arial.ttf"),
        "bold": ("ReportArialBold", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        "head": ("ReportNarrowBold", "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf"),
        "serif": ("ReportGeorgiaBold", "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"),
    }
    for key, (name, path) in locations.items():
        if Path(path).is_file():
            pdfmetrics.registerFont(TTFont(name, path))
            fonts[key] = name
    return fonts


def plain(value):
    return str(value).translate(str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00a0": " ", "\u202f": " "}))


def stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{parsed.strftime('%d %b %Y')} / {parsed.strftime('%H:%M')} UTC"


def date_label(value):
    return date.fromisoformat(value).strftime("%d %B %Y")


class Edition:
    def __init__(self, report, destination):
        self.report = report
        self.fonts = register_fonts()
        self.canvas = canvas.Canvas(destination, pagesize=(WIDTH, HEIGHT), pageCompression=1)
        self.canvas.setTitle(f"{report['event']['name']} - Fight Forecast")
        self.canvas.setAuthor("UFC Prediction Model")
        self.canvas.setSubject("Research bout-winner probabilities, coverage, and retained benchmark evidence")
        self.page = 0
        self.y = HEIGHT

    def text(self, text, x, y, size=10, font="body", color=INK, align="left"):
        self.canvas.setFillColor(color)
        self.canvas.setFont(self.fonts[font], size)
        method = self.canvas.drawRightString if align == "right" else self.canvas.drawString
        method(x, y, plain(text))

    def line(self, x1, y1, x2, y2, color=LINE, width=.6):
        self.canvas.setStrokeColor(color)
        self.canvas.setLineWidth(width)
        self.canvas.line(x1, y1, x2, y2)

    def paragraph(self, text, x, top, width, *, size=9.5, leading=13, color=INK, font="body", draw=True):
        style = ParagraphStyle("report", fontName=self.fonts[font], fontSize=size,
                               leading=leading, textColor=color, alignment=TA_LEFT,
                               splitLongWords=True, allowWidows=0, allowOrphans=0)
        value = html.escape(plain(text)).replace("\n", "<br/>")
        paragraph = Paragraph(value, style)
        _, height = paragraph.wrap(width, 2000)
        if draw:
            paragraph.drawOn(self.canvas, x, top - height)
        return top - height

    def ensure(self, height, continuation="Notes & sources"):
        if self.y - height < BOTTOM:
            self.new_page(continuation)

    def footer(self):
        self.line(MARGIN, 45, WIDTH - MARGIN, 45, INK, 1)
        self.text("UFC PREDICTION MODEL", MARGIN, 29, 7.3, "bold")
        self.text("RESEARCH ESTIMATES / NO LIVE EDGE ESTABLISHED", 204, 29, 6.9, color=MUTED)
        self.text(f"{self.page:02d}", WIDTH - MARGIN, 27.5, 12, "head", align="right")

    def new_page(self, title=None, subtitle=None):
        if self.page:
            self.footer()
            self.canvas.showPage()
        self.page += 1
        self.canvas.setFillColor(PAPER)
        self.canvas.rect(0, 0, WIDTH, HEIGHT, stroke=0, fill=1)
        self.canvas.setFillColor(RED)
        self.canvas.rect(MARGIN, 745, 5, 15, stroke=0, fill=1)
        self.text("FIGHT / FORECAST", MARGIN + 13, 748, 12.5, "head")
        self.text("RESEARCH EDITION", WIDTH - MARGIN, 750, 7.5, "bold", MUTED, "right")
        self.line(MARGIN, 735, WIDTH - MARGIN, 735, INK, 1.1)
        self.y = 711
        if title:
            self.text(title, MARGIN, self.y - 4, 25, "serif")
            self.y -= 27
        if subtitle:
            self.y = self.paragraph(subtitle, MARGIN, self.y, CONTENT, size=9.1, leading=12, color=MUTED)
            self.y -= 14

    def section_label(self, label, number=None):
        self.ensure(35)
        if number:
            self.text(number, MARGIN, self.y, 9, "bold", RED)
        self.text(label.upper(), MARGIN + (25 if number else 0), self.y, 9, "bold", RED)
        self.y -= 14

    def heading(self, title, size=20):
        self.ensure(45)
        self.text(title, MARGIN, self.y, size, "serif")
        self.y -= 18

    def finish(self):
        self.footer()
        self.canvas.save()

    def cover(self):
        report = self.report
        scored, unscored = len(report["bouts"]), len(report["unscored_bouts"])
        self.new_page()
        self.text(date_label(report["event"]["event_date"]).upper(), MARGIN, self.y, 9, "bold", RED)
        self.y -= 12
        self.y = self.paragraph(report["event"]["name"], MARGIN, self.y, CONTENT,
                                size=34, leading=36, font="head")
        self.y -= 10
        self.y = self.paragraph("Pre-fight probabilities. The evidence behind each lean. Athlete statistics, model inputs and sources made visible.",
                                MARGIN, self.y, CONTENT, size=10.5, leading=14, color=MUTED)
        self.y -= 17
        self.line(MARGIN, self.y, WIDTH - MARGIN, self.y, INK, .9)
        top = self.y
        model_short = report["forecast"]["model_version"].split(":")[0].replace("logistic-prior-", "").upper()
        columns = [(f"{scored}/{scored + unscored}", "MATCHUPS SCORED", "All remaining bouts listed on the coverage page"),
                   (model_short, "MODEL EDITION", report["forecast"]["model_label"]),
                   (str(sum(bool(row["market"]) for row in report["bouts"])), "VERIFIED MARKET PAIRS", "Freshness assessed at the forecast cutoff")]
        for index, (value, label, note) in enumerate(columns):
            x = MARGIN + index * CONTENT / 3
            if index:
                self.line(x - 10, top - 13, x - 10, top - 70)
            value_size = 26 if len(value) < 12 else 16
            while value_size > 9 and pdfmetrics.stringWidth(plain(value), self.fonts["head"], value_size) > CONTENT / 3 - 20:
                value_size -= .5
            self.text(value, x, top - 31, value_size, "head")
            self.text(label, x, top - 47, 7.5, "bold", MUTED)
            self.paragraph(note, x, top - 54, CONTENT / 3 - 21, size=7.8, leading=10, color=MUTED)
        self.y = top - 88
        self.text("FORECAST CUTOFF", MARGIN, self.y, 7.6, "bold", RED)
        self.text(stamp(report["forecast"]["cutoff_at_utc"]), MARGIN + 110, self.y, 8.7)
        self.y -= 23
        self.section_label("The scored card", "01")
        self.text("MATCHUP", MARGIN, self.y, 7.2, "bold", MUTED)
        self.text("MODEL FAVORITE", 352, self.y, 7.2, "bold", MUTED)
        self.text("WIN CHANCE", WIDTH - MARGIN, self.y, 7.2, "bold", MUTED, "right")
        self.y -= 9
        self.line(MARGIN, self.y, WIDTH - MARGIN, self.y, INK, .9)
        for bout in report["bouts"]:
            self.ensure(44, "The scored card / continued")
            top = self.y
            self.paragraph(bout["fighter_a"] + " vs. " + bout["fighter_b"], MARGIN, top - 9, 285,
                           size=10.2, leading=12, font="bold")
            favorite, probability, _ = _favorite(bout)
            self.paragraph(favorite, 352, top - 10, 128, size=9.2, leading=11)
            self.text(f"{probability:.1%}", WIDTH - MARGIN, top - 24, 16, "head", RED, "right")
            self.y -= 43
            self.line(MARGIN, self.y, WIDTH - MARGIN, self.y)
        self.y -= 16
        note = "A 60% forecast still assigns the other fighter a 40% chance. Win probability is a model estimate, not a confidence score or a guarantee. The following pages show the factors and available inputs."
        self.ensure(68)
        self.canvas.setFillColor(WASH)
        note_bottom = self.paragraph(note, MARGIN + 12, self.y - 11, CONTENT - 24,
                                     size=9, leading=12, draw=False)
        self.canvas.rect(MARGIN, note_bottom - 11, CONTENT, self.y - note_bottom + 11, stroke=0, fill=1)
        self.paragraph(note, MARGIN + 12, self.y - 11, CONTENT - 24, size=9, leading=12)
        self.y = note_bottom - 20

    def coverage_string(self, bout, side):
        return _coverage_text(bout, side).replace(" · ", " / ")

    def measurement_height(self, bout):
        rows = measurement_rows(bout)
        if not rows:
            return 0
        note_height = -self.paragraph(measurement_note(bout), 0, 0, CONTENT, size=7.7, leading=10, draw=False)
        source_height = 13 * len(bout.get("measurements", {}))
        return 50 + len(rows) * 12 + note_height + source_height + 20

    def measurement_strip(self, bout, top):
        rows = measurement_rows(bout)
        if not rows:
            return top
        self.line(MARGIN, top, WIDTH - MARGIN, top, LINE)
        self.text("OBSERVED ATHLETE STATISTICS", MARGIN, top - 13, 7.4, "bold", RED)
        y = top - 26
        self.text(bout["fighter_a"], MARGIN, y, 8, "bold")
        self.text(bout["fighter_b"], WIDTH - MARGIN, y, 8, "bold", align="right")
        y -= 8
        self.line(MARGIN, y, WIDTH - MARGIN, y)
        for a, label, b in rows:
            y -= 11
            self.text(a, MARGIN, y, 8.3, "bold")
            self.text(label, MARGIN + 158, y, 7.8, color=MUTED)
            self.text(b, WIDTH - MARGIN, y, 8.3, "bold", align="right")
            self.line(MARGIN, y - 4, WIDTH - MARGIN, y - 4, LINE, .35)
            y -= 1
        y -= 13
        y = self.paragraph(measurement_note(bout), MARGIN, y, CONTENT, size=7.7, leading=10, color=MUTED) - 7
        for side in ("fighter_a", "fighter_b"):
            profile = bout.get("measurements", {}).get(side)
            if not profile:
                continue
            label = bout[side] + " / " + profile["source_label"]
            self.text(label, MARGIN, y, 7.1, color=MUTED)
            self.text(stamp(profile["captured_at_utc"]), WIDTH - MARGIN, y, 7.1, color=MUTED, align="right")
            label_width = min(CONTENT * .68, pdfmetrics.stringWidth(plain(label), self.fonts["body"], 7.1))
            self.canvas.linkURL(profile["source_url"], (MARGIN, y - 2, MARGIN + label_width, y + 9), relative=0, thickness=0)
            y -= 13
        return y

    def matchup_height(self, bout):
        factors = _factor_texts(bout) or ["Directional model factors are unavailable for this bout."]
        factor_height = sum(-self.paragraph(text, 0, 0, 308, size=9, leading=12, draw=False) + 4 for text in factors)
        coverage_height = 0
        for side in ("fighter_a", "fighter_b"):
            coverage_height += -self.paragraph(bout[side] + ": " + self.coverage_string(bout, side),
                                               0, 0, 183, size=7.9, leading=10.5, draw=False) + 7
        return max(192, 100 + max(factor_height, coverage_height) + 25) + self.measurement_height(bout)

    def matchup(self, bout, index):
        height = self.matchup_height(bout)
        self.ensure(height + 6, "Matchup intelligence / continued")
        top = self.y
        details = bout.get("weight_class", "Bout forecast")
        if bout.get("scheduled_rounds"):
            details += f" / {bout['scheduled_rounds']} rounds"
        self.text(f"{index:02d}  {details.upper()}", MARGIN, top, 8, "bold", RED)
        _, _, strength = _favorite(bout)
        self.text(strength.upper(), WIDTH - MARGIN, top, 7.5, "bold", MUTED, "right")
        for side, x, width in (("fighter_a", MARGIN, 252), ("fighter_b", 318, 252)):
            name = bout[side]
            size = 20
            while size > 11 and pdfmetrics.stringWidth(plain(name), self.fonts["head"], size) > width:
                size -= .5
            self.text(name, x, top - 25, size, "head")
            key = "p_fighter_a" if side == "fighter_a" else "p_fighter_b"
            self.text(f"{bout[key]:.1%}", x, top - 57, 29, "head", RED if bout[key] >= .5 else INK)
            self.text("WIN PROBABILITY", x + 94, top - 52, 6.9, "bold", MUTED)
        bar_y = top - 73
        self.canvas.setFillColor(LINE)
        self.canvas.rect(MARGIN, bar_y, CONTENT, 6, stroke=0, fill=1)
        self.canvas.setFillColor(RED)
        if bout["p_fighter_a"] >= .5:
            self.canvas.rect(MARGIN, bar_y, CONTENT * bout["p_fighter_a"], 6, stroke=0, fill=1)
        else:
            self.canvas.rect(MARGIN + CONTENT * bout["p_fighter_a"], bar_y,
                             CONTENT * bout["p_fighter_b"], 6, stroke=0, fill=1)
        self.line(MARGIN + CONTENT / 2, bar_y - 2, MARGIN + CONTENT / 2, bar_y + 8, PAPER, 1)
        self.text("DIRECTIONAL FACTORS", MARGIN, top - 91, 7, "bold", MUTED)
        self.text("INPUT COVERAGE", 387, top - 91, 7, "bold", MUTED)
        factor_y = top - 101
        for text in _factor_texts(bout) or ["Directional model factors are unavailable for this bout."]:
            self.text("/", MARGIN, factor_y - 8, 9, "bold", RED)
            factor_y = self.paragraph(text, MARGIN + 12, factor_y, 308, size=9, leading=12) - 4
        coverage_y = top - 101
        for side in ("fighter_a", "fighter_b"):
            coverage_y = self.paragraph(bout[side] + ": " + self.coverage_string(bout, side),
                                        387, coverage_y, 183, size=7.9, leading=10.5, color=MUTED) - 7
        market_y = min(factor_y, coverage_y) - 4
        market = bout["market"]
        if market:
            market_text = f"Verified market at cutoff / {market['bookmaker']}: {bout['fighter_a']} {market['p_fighter_a']:.1%}, {bout['fighter_b']} {market['p_fighter_b']:.1%} (no vig)."
        else:
            market_text = "Market comparison unavailable: no verified two-sided price fresh at this forecast cutoff."
        market_bottom = self.paragraph(market_text, MARGIN, market_y, CONTENT, size=7.7, leading=10, color=MUTED)
        stats_bottom = self.measurement_strip(bout, market_bottom - 13)
        self.y = min(top - height, stats_bottom - 15) if measurement_rows(bout) else top - height
        self.line(MARGIN, self.y + 9, WIDTH - MARGIN, self.y + 9)

    def matchups(self):
        self.new_page("Matchup intelligence", "Probability and evidence are separate. Factors describe the fitted model's direction; they do not prove causation.")
        for index, bout in enumerate(self.report["bouts"], 1):
            self.matchup(bout, index)

    def gaps(self):
        self.new_page("The complete coverage picture", "A source-listed matchup can lack a publishable model forecast when stable research identities are unresolved.")
        scored, missing = len(self.report["bouts"]), len(self.report["unscored_bouts"])
        total = scored + missing
        self.text(f"{scored} SCORED", MARGIN, self.y, 12, "head", RED)
        self.text(f"{missing} NOT SCORED", WIDTH - MARGIN, self.y, 12, "head", MUTED, "right")
        self.y -= 17
        self.canvas.setFillColor(LINE)
        self.canvas.rect(MARGIN, self.y, CONTENT, 9, stroke=0, fill=1)
        self.canvas.setFillColor(RED)
        self.canvas.rect(MARGIN, self.y, CONTENT * scored / total, 9, stroke=0, fill=1)
        self.y -= 25
        self.y = self.paragraph("No probability is assigned to the matchups below. Missing predictions are not even-money picks and do not imply either fighter is less capable.",
                                MARGIN, self.y, CONTENT, size=9.5, leading=13, color=MUTED)
        self.y -= 21
        self.section_label("Unscored matchups", "02")
        if not missing:
            self.y = self.paragraph("Every matchup in this report has a research forecast. Optional input gaps remain visible on each scored bout.", MARGIN, self.y, CONTENT)
        for index, bout in enumerate(self.report["unscored_bouts"], 1):
            reason_height = -self.paragraph(bout["reason"], 0, 0, CONTENT - 30,
                                             size=8.5, leading=11, draw=False)
            title = bout["fighter_a"] + " vs. " + bout["fighter_b"]
            title_height = -self.paragraph(title, 0, 0, CONTENT - 105,
                                            size=11.4, leading=13, font="bold", draw=False)
            height = title_height + reason_height + 22
            self.ensure(height + 5, "Unscored matchups / continued")
            self.text(f"{index:02d}", MARGIN, self.y - 11, 9, "bold", RED)
            self.paragraph(title, MARGIN + 28, self.y, CONTENT - 105,
                           size=11.4, leading=13, font="bold")
            self.text("NOT SCORED", WIDTH - MARGIN, self.y - 10, 6.9, "bold", MUTED, "right")
            self.paragraph(bout["reason"], MARGIN + 28, self.y - title_height - 5,
                           CONTENT - 30, size=8.5, leading=11, color=MUTED)
            self.y -= height
            self.line(MARGIN + 28, self.y + 5, WIDTH - MARGIN, self.y + 5)
    def metrics(self):
        evaluation = self.report.get("evaluation")
        self.new_page("What supports the forecast", "Saved evaluation, the model's limits, and the public source trail.")
        self.section_label("Retrospective evaluation", "03")
        if evaluation:
            self.text(f"{evaluation['bouts']:,} BOUTS", MARGIN, self.y, 9, "bold", RED)
            self.text(evaluation.get("period", evaluation["label"]), WIDTH - MARGIN, self.y, 8, color=MUTED, align="right")
            self.y -= 23
            metrics = [(f"{evaluation['accuracy']:.2%}", "WINNER ACCURACY"),
                       (f"{evaluation['log_loss']:.5f}", "CALIBRATED LOG LOSS"),
                       (f"{evaluation['brier_score']:.5f}", "BRIER SCORE")]
            for index, (value, label) in enumerate(metrics):
                x = MARGIN + CONTENT * index / 3
                self.text(value, x, self.y, 25, "head")
                self.text(label, x, self.y - 15, 7, "bold", MUTED)
            self.y -= 35
            if "baseline_log_loss" in evaluation:
                delta = evaluation["log_loss"] - evaluation["baseline_log_loss"]
                baseline = evaluation.get("baseline_label", "Baseline")
                self.y = self.paragraph(f"{baseline} log loss: {evaluation['baseline_log_loss']:.5f}. Current model: {evaluation['log_loss']:.5f}. Absolute difference: {delta:+.5f}. Lower loss is better.",
                                        MARGIN, self.y, CONTENT, size=9.3, leading=13)
                self.y -= 8
            if evaluation.get("paired_log_loss_interval"):
                low, high = evaluation["paired_log_loss_interval"]
                label = f"95% paired event-date bootstrap interval / log-loss difference: [{low:+.5f}, {high:+.5f}]."
                self.y = self.paragraph(label, MARGIN, self.y, CONTENT, size=9.3, leading=13, color=RED, font="bold")
                self.y -= 9
                self.y = self.paragraph("This measures conditional resampling variation in the retained benchmark. It does not account for dataset, identity, model-selection, or source-timing uncertainty.",
                                        MARGIN, self.y, CONTENT, size=8.5, leading=11.5, color=MUTED)
                self.y -= 12
            rolling = evaluation.get("rolling")
            recent = evaluation.get("recent_year")
            if rolling or recent:
                self.line(MARGIN, self.y, WIDTH - MARGIN, self.y)
                self.y -= 14
                recent_label = (f"{recent['year']} / weaker current year" if recent and recent['log_loss'] > evaluation['log_loss']
                                else f"{recent['year']} fold" if recent else "Recent year")
                for label, value in (("Rolling origin", rolling), (recent_label, recent)):
                    if value:
                        self.text(label, MARGIN, self.y, 8.8, "bold")
                        self.text(f"{value['bouts']:,} bouts / {value['accuracy']:.2%} accuracy / {value['log_loss']:.5f} loss",
                                  WIDTH - MARGIN, self.y, 8.5, color=MUTED, align="right")
                        self.y -= 16
                self.y -= 2
            if evaluation.get("note"):
                self.y = self.paragraph(evaluation["note"], MARGIN, self.y, CONTENT, size=8.6, leading=11.5, color=MUTED)
                self.y -= 16
        else:
            self.y = self.paragraph("No saved evaluation was supplied with this public forecast. Model quality cannot be inferred from the displayed probabilities.", MARGIN, self.y, CONTENT)
            self.y -= 17
        self.section_label("Reading the model")
        description = "The model estimates the bout winner from earlier UFC results, Elo history, experience, recent form, rest, and available birth dates. An age-above-32 term allows an additional age-related effect. Saved coverage identifies which additional reach or earlier-fight statistics were available to this fitted model. Observed current career averages are shown separately and do not become historical training evidence. Missing optional inputs contribute neutrally. Coverage describes available evidence, not a fighter's ability."
        common_notes = [note for note in self.report["bouts"][0]["notes"]
                        if all(note in bout["notes"] for bout in self.report["bouts"])]
        if common_notes:
            description += " " + " ".join(dict.fromkeys(common_notes))
        self.y = self.paragraph(description, MARGIN, self.y, CONTENT, size=8.7, leading=12, color=MUTED)
        self.y -= 15
        notes = self.report["limitations"]
        if notes:
            self.section_label("Forecast limits")
            for item in notes:
                height = -self.paragraph(item, 0, 0, CONTENT - 12, size=8.1, leading=11, draw=False)
                self.ensure(height + 7)
                self.text("/", MARGIN, self.y - 8, 8.3, "bold", RED)
                self.y = self.paragraph(item, MARGIN + 12, self.y, CONTENT - 12, size=8.1, leading=11, color=MUTED) - 5
        sources = list(self.report["sources"])
        if self.report["event"].get("source_url") and not any(s["url"] == self.report["event"]["source_url"] for s in sources):
            sources.insert(0, {"label": "Event card source", "url": self.report["event"]["source_url"]})
        if sources:
            def source_height(source, width):
                return (-self.paragraph(source["label"], 0, 0, width, size=7.7, leading=10, font="bold", draw=False)
                        - self.paragraph(source["url"], 0, 0, width, size=7.1, leading=9, draw=False) + 10)
            def source_card(index, source, x, top, width):
                top = self.paragraph(f"{index:02d}  {source['label']}", x, top, width, size=7.7, leading=10, font="bold") - 2
                start = top
                top = self.paragraph(source["url"], x, top, width, size=7.1, leading=9, color=MUTED)
                self.canvas.linkURL(source["url"], (x, top, x + width, start), relative=0, thickness=0)
                return top - 8
            column_width = (CONTENT - 24) / 2
            split = (len(sources) + 1) // 2
            columns = (list(enumerate(sources[:split], 1)), list(enumerate(sources[split:], split + 1)))
            grid_height = max(sum(source_height(source, column_width) for _, source in column) for column in columns)
            if grid_height + 45 > self.y - BOTTOM:
                self.new_page("Public sources & provenance", "Public links identify the retained source observations; they do not establish operating approval.")
            else:
                self.y -= 6
                self.section_label("Public sources")
            if grid_height + 24 <= self.y - BOTTOM:
                ends = []
                for column_index, column in enumerate(columns):
                    y = self.y
                    for index, source in column:
                        y = source_card(index, source, MARGIN + column_index * (column_width + 24), y, column_width)
                    ends.append(y)
                self.y = min(ends)
            else:
                for index, source in enumerate(sources, 1):
                    self.ensure(source_height(source, CONTENT))
                    self.y = source_card(index, source, MARGIN, self.y, CONTENT)
        if self.report["source_notes"]:
            self.y -= 17
            self.section_label("Card, capture & input notes")
            for item in self.report["source_notes"]:
                height = -self.paragraph(item, 0, 0, CONTENT - 12, size=7.7, leading=10.2, draw=False)
                self.ensure(height + 5, "Card & capture notes / continued")
                self.text("/", MARGIN, self.y - 8, 8, "bold", RED)
                self.y = self.paragraph(item, MARGIN + 12, self.y, CONTENT - 12,
                                        size=7.7, leading=10.2, color=MUTED) - 4
        bout_notes = [(bout, note) for bout in self.report["bouts"] for note in bout["notes"] if note not in common_notes]
        if bout_notes:
            self.y -= 5
            self.section_label("Additional bout notes")
            for bout, note in bout_notes:
                text = bout["fighter_a"] + " vs. " + bout["fighter_b"] + ": " + note
                height = -self.paragraph(text, 0, 0, CONTENT, size=8, leading=11, draw=False)
                self.ensure(height + 6)
                self.y = self.paragraph(text, MARGIN, self.y, CONTENT, size=8, leading=11, color=MUTED) - 5
        self.ensure(24)
        self.y -= 4
        self.line(MARGIN, self.y, WIDTH - MARGIN, self.y, INK, .9)
        self.y -= 10
        self.paragraph("Prepared " + stamp(self.report["generated_at_utc"]) + ". " + self.report["forecast"]["model_version"],
                       MARGIN, self.y, CONTENT, size=7.2, leading=10, color=MUTED)


def render_pdf(payload, output):
    """Validate the public payload before exclusive-creating any PDF output."""
    report = validate_report(payload)
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Refusing to replace existing PDF: {output.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    # A file object opened with xb provides an atomic no-overwrite boundary.
    with output.open("xb") as stream:
        edition = Edition(report, stream)
        edition.cover()
        edition.matchups()
        edition.gaps()
        edition.metrics()
        edition.finish()
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Canonical public forecast JSON")
    parser.add_argument("--output", required=True, type=Path, help="New PDF destination")
    args = parser.parse_args(argv)
    with args.input.open() as stream:
        payload = json.load(stream)
    destination = render_pdf(payload, args.output)
    print(f"Created forecast PDF: {destination.name}")


if __name__ == "__main__":
    main()
