"""
Month-End Close Assistant - Step 8: close report and decision package.

A presentation layer over artefacts that already exist. The rule it must never
break:

    the report may count, total, sort, filter and format;
    it may never re-derive an accounting outcome.

No re-matching, no re-conversion, no recomputed elimination, and no second
opinion on whether a control passed. Every figure below is a formatted value or
a plain aggregate of a column produced by ``match.py``, ``intercompany.py`` or
``controls.py``. No language model is involved in any number or any sentence.

Layers
------
``build_report_model``     pure: artefacts in, ReportModel out. No file I/O.
``render_pdf``             the only function that touches reportlab.
``write_decision_package`` PDF + control results CSV + decision JSON + manifest.
``__main__``               CLI: loads the files and calls the three above.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import reportlab.rl_config as rl_config

# Byte-identical output for identical inputs; must be set before any canvas
# is created, which is why it sits at import time.
rl_config.invariant = 1

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfgen import canvas as pdfcanvas  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from plant_downstream_errors import ProtectedPathError, assert_writable  # noqa: E402,F401

REPORT_VERSION = "1.0.0"

# status bands -------------------------------------------------------------
BAND_PASS = "APPROVED FOR CLOSE"
BAND_WARN = "APPROVED WITH WARNINGS"
BAND_BLOCKED = "CLOSE BLOCKED - NOT APPROVED"

TONE_PASS = "pass"
TONE_WARN = "warning"
TONE_BLOCKED = "blocked"

COLOURS = {
    TONE_PASS: colors.HexColor("#1B7F4B"),
    TONE_WARN: colors.HexColor("#B37400"),
    TONE_BLOCKED: colors.HexColor("#B3261E"),
    "rule": colors.HexColor("#9AA0A6"),
    "header": colors.HexColor("#1F2933"),
    "band_text": colors.white,
    "muted": colors.HexColor("#5F6368"),
    "row": colors.HexColor("#F1F3F4"),
}

STEM_REPORT = "close_report"
STEM_EXCEPTION = "close_exception_report"

NO_LLM_NOTE = (
    "All figures in this document are produced by deterministic Python from the "
    "source files listed in the provenance appendix. No language model is used in "
    "any calculation, control evaluation or sentence of this report."
)

FX_NOTE = (
    "Intercompany elimination cannot detect an incorrect FX rate: both sides of a "
    "recharge use the same rate, so receivables and payables still agree. The "
    "source-accuracy controls below compare the supplied rates with an independent "
    "reference and re-perform each booked amount at that reference."
)


# ---------------------------------------------------------------------------
# formatting helpers - amounts and rates never share a formatter
# ---------------------------------------------------------------------------
def fmt_amount(value: Any) -> str:
    number = _to_number(value)
    return "-" if number is None else f"{number:,.2f}"


def fmt_rate(value: Any) -> str:
    number = _to_number(value)
    return "-" if number is None else f"{number:.4f}"


def fmt_count(value: Any) -> str:
    number = _to_number(value)
    return "-" if number is None else f"{int(number):,}"


def _to_number(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def slugify(label: str) -> str:
    """Lowercase, non-alphanumerics to '-', so a label can never escape its directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")
    return slug or "unlabelled"


def _month_key(value: Any) -> str | None:
    text = _text(value).strip()
    if not text:
        return None
    try:
        return pd.Period(pd.to_datetime(text), freq="M").strftime("%Y-%m")
    except Exception:  # noqa: BLE001
        return None


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Headline:
    band_text: str
    tone: str
    summary: str


@dataclass(frozen=True)
class ExceptionTable:
    rows: list[dict]
    shown: int
    total: int

    @property
    def omitted(self) -> int:
        return max(self.total - self.shown, 0)

    @property
    def more_note(self) -> str:
        if self.omitted <= 0:
            return ""
        return f"and {self.omitted} more - see control_results.csv for the full population"


@dataclass(frozen=True)
class ReportModel:
    dataset_label: str
    run_timestamp: str
    controls_version: str
    report_version: str
    config_used: dict
    decision: dict
    headline: Headline
    period: str
    entities: list[str]
    cover_figures: list[tuple[str, str]]
    blocking_explanations: list[dict]
    control_summary: list[dict]
    family_summary: list[dict]
    failed_controls: list[dict]
    warnings: list[dict]
    matching_summary: list[tuple[str, str]]
    matching_status_counts: list[dict]
    matching_exceptions: ExceptionTable
    intercompany_summary: list[tuple[str, str]]
    intercompany_by_entity: list[dict]
    intercompany_exceptions: ExceptionTable
    fx_summary: list[dict]
    fx_control_summary: list[dict]
    control_results_records: list[dict] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)

    @property
    def report_allowed(self) -> bool:
        return bool(self.decision.get("report_allowed"))

    @property
    def filename_stem(self) -> str:
        stem = STEM_REPORT if self.report_allowed else STEM_EXCEPTION
        return f"{stem}_{slugify(self.dataset_label)}"


def _headline(decision: dict) -> Headline:
    status = decision.get("close_status")
    blocking = decision.get("blocking_controls") or []
    if status == "PASS":
        return Headline(BAND_PASS, TONE_PASS,
                        "All critical controls passed. The close may be reported.")
    if status == "PASS_WITH_WARNINGS":
        return Headline(
            BAND_WARN, TONE_WARN,
            f"All critical controls passed. {decision.get('warnings', 0)} warning(s) "
            "require review but do not block the close.",
        )
    return Headline(
        BAND_BLOCKED, TONE_BLOCKED,
        f"{decision.get('critical_failed', 0)} critical control(s) failed"
        + (f" ({', '.join(blocking)})." if blocking else ".")
        + " The close is not approved and no approved report is issued.",
    )


def _value_by_currency(rows: pd.DataFrame) -> list[tuple[str, float]]:
    """Total invoice value per currency.

    Amounts in different currencies are never summed into one figure: a blended
    EUR + USD total is not an economically meaningful number, and translating it
    here would mean the report performing a conversion, which is exactly what a
    presentation layer must not do.
    """
    if rows is None or rows.empty or "invoice_currency" not in rows.columns:
        return []
    work = rows.copy()
    work["_value"] = pd.to_numeric(work.get("invoice_total_amount"), errors="coerce")
    totals = work.groupby(work["invoice_currency"].fillna("(unknown)"))["_value"].sum()
    return [(str(currency), float(total)) for currency, total in totals.sort_index().items()]


def _records(frame: pd.DataFrame, columns: Sequence[str]) -> list[dict]:
    """Rows as plain dicts, keeping only the columns that exist."""
    if frame is None or frame.empty:
        return []
    keep = [c for c in columns if c in frame.columns]
    return frame[keep].fillna("").to_dict(orient="records")


def _capped(frame: pd.DataFrame, columns: Sequence[str], limit: int) -> ExceptionTable:
    total = 0 if frame is None else len(frame)
    if total == 0:
        return ExceptionTable([], 0, 0)
    rows = _records(frame.head(limit), columns)
    return ExceptionTable(rows, len(rows), total)


def build_report_model(
    match_results: pd.DataFrame,
    invoices: pd.DataFrame,
    payments: pd.DataFrame,
    ic_entries: pd.DataFrame,
    ic_elimination: pd.DataFrame,
    shared_costs: pd.DataFrame,
    fx_actual: pd.DataFrame,
    fx_reference: pd.DataFrame,
    control_results: pd.DataFrame,
    decision: dict,
    inputs: list[dict] | None = None,
    max_exception_rows: int = 25,
    period: str = "January - March 2026",
) -> ReportModel:
    """Assemble everything the report shows. Pure: no file I/O, no mutation."""
    results = control_results.copy()
    entries = ic_entries.copy()
    accounting = entries[entries["entry_type"].isin(["RECEIVABLE", "PAYABLE"])]

    # --- controls ----------------------------------------------------------
    summary_columns = ["check_id", "check_name", "check_group", "severity", "status",
                       "expected_value", "actual_value", "difference", "failed_count",
                       "explanation"]
    failed = results[results["status"].isin(["FAIL", "ERROR"])].copy()
    failed["_order"] = (failed["severity"] != "CRITICAL").astype(int)
    failed = failed.sort_values(["_order", "check_id"], kind="mergesort")
    warnings = results[results["status"] == "WARNING"]

    group_rows: list[dict] = []
    for group, block in results.groupby("check_group", sort=True):
        counts = block["status"].value_counts()
        group_rows.append({
            "group": group,
            "controls": len(block),
            "pass": int(counts.get("PASS", 0)),
            "warning": int(counts.get("WARNING", 0)),
            "fail": int(counts.get("FAIL", 0)),
            "error": int(counts.get("ERROR", 0)),
        })

    family_rows: list[dict] = []
    for family, block in results.groupby("control_family", sort=True):
        counts = block["status"].value_counts()
        family_rows.append({
            "family": family,
            "controls": len(block),
            "pass": int(counts.get("PASS", 0)),
            "not_passing": int(len(block) - counts.get("PASS", 0)),
        })

    blocking_ids = decision.get("blocking_controls") or []
    blocking_explanations = _records(
        results[results["check_id"].isin(blocking_ids)],
        ["check_id", "check_name", "explanation"],
    )

    # --- matching ----------------------------------------------------------
    invoice_rows = match_results[match_results["record_type"] == "INVOICE"]
    orphan_rows = match_results[match_results["record_type"] == "PAYMENT_ONLY"]
    matched = invoice_rows[invoice_rows["status"] == "MATCHED"]
    exceptions = match_results[match_results["status"] != "MATCHED"]

    matching_summary = [
        ("Source invoice rows", fmt_count(len(invoices))),
        ("Source payment rows", fmt_count(len(payments))),
        ("Result rows (invoices + orphan payment groups)", fmt_count(len(match_results))),
        ("Invoices matched", fmt_count(len(matched))),
        ("Exceptions", fmt_count(len(exceptions))),
    ]
    # Matched value is reported per currency and never as one blended figure:
    # adding EUR and USD amounts together would produce a number that means
    # nothing, and the invoice population spans both.
    matched_value_by_currency = _value_by_currency(matched)
    for currency, amount in matched_value_by_currency:
        matching_summary.append((f"Matched invoice value ({currency})", fmt_amount(amount)))
    status_counts = [
        {"status": status, "rows": fmt_count(count)}
        for status, count in match_results["status"].value_counts().sort_index().items()
    ]
    matching_exceptions = _capped(
        exceptions,
        ["invoice_id", "po_id", "entity", "po_net_amount", "invoice_net_amount",
         "total_paid", "status", "status_reason"],
        max_exception_rows,
    )

    # --- intercompany ------------------------------------------------------
    receivable = pd.to_numeric(
        accounting.loc[accounting["entry_type"] == "RECEIVABLE", "eur_equivalent"],
        errors="coerce",
    ).sum()
    payable = pd.to_numeric(
        accounting.loc[accounting["entry_type"] == "PAYABLE", "eur_equivalent"],
        errors="coerce",
    ).sum()
    pairs_ok = 0 if ic_elimination.empty else int((ic_elimination["check_status"] == "OK").sum())
    intercompany_summary = [
        ("Shared costs processed", fmt_count(len(shared_costs))),
        ("Intercompany entries", fmt_count(len(accounting))),
        ("Intercompany pairs", fmt_count(0 if ic_elimination.empty else len(ic_elimination))),
        ("Receivables (EUR)", fmt_amount(receivable)),
        ("Payables (EUR)", fmt_amount(payable)),
        ("Net position (EUR, expected 0.00)", fmt_amount(receivable - payable)),
        ("Pairs eliminating within tolerance", fmt_count(pairs_ok)),
    ]

    by_entity: list[dict] = []
    if not accounting.empty:
        work = accounting.copy()
        work["eur"] = pd.to_numeric(work["eur_equivalent"], errors="coerce")
        for entity, block in work.groupby("entity", sort=True):
            entity_receivable = block.loc[block["entry_type"] == "RECEIVABLE", "eur"].sum()
            entity_payable = block.loc[block["entry_type"] == "PAYABLE", "eur"].sum()
            by_entity.append({
                "entity": entity,
                "entries": fmt_count(len(block)),
                "receivable_eur": fmt_amount(entity_receivable),
                "payable_eur": fmt_amount(entity_payable),
                "net_eur": fmt_amount(entity_receivable - entity_payable),
            })

    elimination_issues = (
        ic_elimination[ic_elimination["check_status"] != "OK"]
        if not ic_elimination.empty
        else ic_elimination
    )
    cost_exceptions = entries[entries["entry_type"] == "EXCEPTION"]
    combined_issues = pd.concat(
        [
            elimination_issues.assign(reference=elimination_issues.get("pair_id"))
            if not elimination_issues.empty
            else elimination_issues,
            cost_exceptions.assign(
                reference=cost_exceptions.get("cost_id"),
                check_status=cost_exceptions.get("status"),
                check_reason=cost_exceptions.get("status_reason"),
            )
            if not cost_exceptions.empty
            else cost_exceptions,
        ],
        ignore_index=True,
    ) if (len(elimination_issues) or len(cost_exceptions)) else pd.DataFrame()
    intercompany_exceptions = _capped(
        combined_issues,
        ["reference", "month", "paying_entity", "receiving_entity",
         "receivable_eur", "payable_eur", "difference_eur", "check_status", "check_reason"],
        max_exception_rows,
    )

    # --- FX ----------------------------------------------------------------
    def _rates(frame: pd.DataFrame) -> dict[str, float | None]:
        table: dict[str, float | None] = {}
        for row in frame.itertuples():
            key = _month_key(getattr(row, "month_end", None))
            if key and key not in table:
                table[key] = _to_number(getattr(row, "eur_usd", None))
        return table

    supplied = _rates(fx_actual)
    reference_rates = _rates(fx_reference)
    tolerance = float(decision.get("config_used", {}).get("fx_comparison_tolerance", 1e-6))

    fx_summary: list[dict] = []
    for month in sorted(set(supplied) | set(reference_rates)):
        actual_rate = supplied.get(month)
        reference_rate = reference_rates.get(month)
        if actual_rate is None:
            verdict, difference = "MISSING FROM FX FILE", None
        elif reference_rate is None:
            verdict, difference = "NOT IN REFERENCE", None
        else:
            difference = actual_rate - reference_rate
            verdict = "OK" if abs(difference) <= tolerance else "DIFFERS FROM REFERENCE"
        fx_summary.append({
            "month": month,
            "supplied": fmt_rate(actual_rate),
            "reference": fmt_rate(reference_rate),
            "difference": "-" if difference is None else f"{difference:+.4f}",
            "verdict": verdict,
        })

    fx_controls = _records(
        results[results["check_group"] == "FXC"],
        ["check_id", "check_name", "severity", "status", "explanation"],
    )

    # --- cover -------------------------------------------------------------
    passed_controls = int((results["status"] == "PASS").sum())
    cover_figures = [
        ("Invoices processed", fmt_count(len(invoice_rows))),
        ("Matching exceptions", fmt_count(len(exceptions))),
        ("Intercompany pairs", fmt_count(0 if ic_elimination.empty else len(ic_elimination))),
        ("Net intercompany position (EUR)", fmt_amount(receivable - payable)),
        ("Controls passed", f"{passed_controls} of {len(results)}"),
    ]

    return ReportModel(
        dataset_label=str(decision.get("dataset_label") or "unlabelled"),
        run_timestamp=str(decision.get("run_timestamp") or ""),
        controls_version=str(decision.get("controls_version") or ""),
        report_version=REPORT_VERSION,
        config_used=dict(decision.get("config_used") or {}),
        decision=dict(decision),
        headline=_headline(decision),
        period=period,
        entities=sorted((decision.get("config_used") or {}).get("entity_currency", {})),
        cover_figures=cover_figures,
        blocking_explanations=blocking_explanations,
        control_summary=group_rows,
        family_summary=family_rows,
        failed_controls=_records(failed, summary_columns),
        warnings=_records(warnings, summary_columns),
        matching_summary=matching_summary,
        matching_status_counts=status_counts,
        matching_exceptions=matching_exceptions,
        intercompany_summary=intercompany_summary,
        intercompany_by_entity=by_entity,
        intercompany_exceptions=intercompany_exceptions,
        fx_summary=fx_summary,
        fx_control_summary=fx_controls,
        control_results_records=results.fillna("").to_dict(orient="records"),
        inputs=list(inputs or []),
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
class _NumberedCanvas(pdfcanvas.Canvas):
    """Two-pass canvas so the footer can print 'page x of y'."""

    def __init__(self, *args, footer_left: str = "", footer_right: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_states: list[dict] = []
        self._footer_left = footer_left
        self._footer_right = footer_right

    def showPage(self) -> None:  # noqa: N802 - reportlab API
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total: int) -> None:
        self.saveState()
        self.setStrokeColor(COLOURS["rule"])
        self.setLineWidth(0.4)
        self.line(18 * mm, 16 * mm, A4[0] - 18 * mm, 16 * mm)
        self.setFont("Helvetica", 7.5)
        self.setFillColor(COLOURS["muted"])
        self.drawString(18 * mm, 11 * mm, self._footer_left)
        self.drawRightString(A4[0] - 18 * mm, 11 * mm,
                             f"{self._footer_right}  |  page {self._pageNumber} of {total}")
        self.restoreState()


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontName="Helvetica-Bold",
                                fontSize=20, leading=24, alignment=TA_LEFT,
                                textColor=COLOURS["header"], spaceAfter=2),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"], fontSize=10,
                                   leading=14, textColor=COLOURS["muted"]),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold",
                             fontSize=13, leading=16, spaceBefore=10, spaceAfter=6,
                             textColor=COLOURS["header"]),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9, leading=13),
        "small": ParagraphStyle("small", parent=base["Normal"], fontSize=7.5, leading=10,
                                textColor=COLOURS["muted"]),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=7.5, leading=9.5),
    }


def _table(data: list[list], widths: list[float], align_right: Sequence[int] = ()) -> Table:
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    style = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), COLOURS["header"]),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, COLOURS["rule"]),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, COLOURS["row"]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for column in align_right:
        style.append(("ALIGN", (column, 0), (column, -1), "RIGHT"))
    table.setStyle(TableStyle(style))
    return table


def _band(model: ReportModel, width: float) -> Table:
    colour = COLOURS[model.headline.tone]
    band = Table([[model.headline.band_text]], colWidths=[width], rowHeights=[16 * mm])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colour),
        ("TEXTCOLOR", (0, 0), (-1, -1), COLOURS["band_text"]),
        ("FONT", (0, 0), (-1, -1), "Helvetica-Bold", 16),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return band


def _story(model: ReportModel, width: float) -> list:
    s = _styles()
    para = lambda text, style="cell": Paragraph(_text(text), s[style])  # noqa: E731
    story: list = []

    # --- cover -------------------------------------------------------------
    document_name = "Close Report" if model.report_allowed else "Close Exception Report"
    story.append(Paragraph(f"Month-End {document_name}", s["title"]))
    story.append(Paragraph(
        f"{model.period} &nbsp;|&nbsp; entities: {', '.join(model.entities) or 'n/a'} "
        f"&nbsp;|&nbsp; dataset: {model.dataset_label}", s["subtitle"]))
    story.append(Spacer(1, 8))
    story.append(_band(model, width))
    story.append(Spacer(1, 6))
    story.append(Paragraph(model.headline.summary, s["body"]))
    story.append(Paragraph(
        f"close_status: {model.decision.get('close_status')} &nbsp;&nbsp; "
        f"report_allowed: {str(model.report_allowed).lower()}", s["body"]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Executive summary", s["h2"]))
    story.append(_table(
        [["Measure", "Value"]] + [[label, value] for label, value in model.cover_figures],
        [width * 0.6, width * 0.4], align_right=(1,)))

    if model.blocking_explanations:
        story.append(Paragraph("Blocking controls", s["h2"]))
        rows = [["Control", "Name", "Finding"]] + [
            [row.get("check_id"), para(row.get("check_name")), para(row.get("explanation"))]
            for row in model.blocking_explanations
        ]
        story.append(_table(rows, [width * 0.12, width * 0.28, width * 0.60]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "This close is not approved. Resolve the findings above and re-run the "
            "close before reporting.", s["body"]))

    # --- controls ----------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Control summary", s["h2"]))
    story.append(_table(
        [["Group", "Controls", "Pass", "Warning", "Fail", "Error"]] + [
            [row["group"], fmt_count(row["controls"]), fmt_count(row["pass"]),
             fmt_count(row["warning"]), fmt_count(row["fail"]), fmt_count(row["error"])]
            for row in model.control_summary],
        [width * 0.2] + [width * 0.16] * 5, align_right=(1, 2, 3, 4, 5)))

    story.append(Paragraph("By control family", s["h2"]))
    story.append(_table(
        [["Family", "Controls", "Pass", "Not passing"]] + [
            [row["family"], fmt_count(row["controls"]), fmt_count(row["pass"]),
             fmt_count(row["not_passing"])] for row in model.family_summary],
        [width * 0.4, width * 0.2, width * 0.2, width * 0.2], align_right=(1, 2, 3)))

    story.append(Paragraph("Failed controls", s["h2"]))
    if model.failed_controls:
        rows = [["Control", "Severity", "Expected", "Actual", "Finding"]] + [
            [para(f"{row.get('check_id')}<br/>{row.get('check_name')}"),
             row.get("severity"), _text(row.get("expected_value")),
             _text(row.get("actual_value")), para(row.get("explanation"))]
            for row in model.failed_controls]
        story.append(_table(rows, [width * 0.20, width * 0.11, width * 0.11,
                                   width * 0.11, width * 0.47]))
    else:
        story.append(Paragraph("None. Every critical control passed.", s["body"]))

    story.append(Paragraph("Warnings", s["h2"]))
    if model.warnings:
        rows = [["Control", "Observation"]] + [
            [para(f"{row.get('check_id')} {row.get('check_name')}"), para(row.get("explanation"))]
            for row in model.warnings]
        story.append(_table(rows, [width * 0.3, width * 0.7]))
    else:
        story.append(Paragraph("None.", s["body"]))

    # --- matching ----------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Invoice matching", s["h2"]))
    story.append(_table(
        [["Measure", "Value"]] + [[label, value] for label, value in model.matching_summary],
        [width * 0.6, width * 0.4], align_right=(1,)))
    story.append(Paragraph("Rows by match status", s["h2"]))
    story.append(_table(
        [["Status", "Rows"]] + [[row["status"], row["rows"]] for row in model.matching_status_counts],
        [width * 0.6, width * 0.4], align_right=(1,)))

    story.append(Paragraph("Matching exceptions", s["h2"]))
    if model.matching_exceptions.rows:
        rows = [["Invoice", "PO", "Entity", "PO net", "Invoice net", "Paid", "Status", "Reason"]]
        for row in model.matching_exceptions.rows:
            rows.append([
                _text(row.get("invoice_id")), _text(row.get("po_id")), _text(row.get("entity")),
                fmt_amount(row.get("po_net_amount")), fmt_amount(row.get("invoice_net_amount")),
                fmt_amount(row.get("total_paid")), _text(row.get("status")),
                para(row.get("status_reason")),
            ])
        story.append(_table(rows, [width * 0.11, width * 0.09, width * 0.07, width * 0.10,
                                   width * 0.10, width * 0.10, width * 0.14, width * 0.29],
                            align_right=(3, 4, 5)))
        if model.matching_exceptions.more_note:
            story.append(Paragraph(model.matching_exceptions.more_note, s["small"]))
    else:
        story.append(Paragraph("None. Every invoice matched its purchase order and payment.",
                               s["body"]))

    # --- intercompany ------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Intercompany recharges", s["h2"]))
    story.append(_table(
        [["Measure", "Value"]] + [[label, value] for label, value in model.intercompany_summary],
        [width * 0.6, width * 0.4], align_right=(1,)))

    if model.intercompany_by_entity:
        story.append(Paragraph("Position by entity", s["h2"]))
        story.append(_table(
            [["Entity", "Entries", "Receivable (EUR)", "Payable (EUR)", "Net (EUR)"]] + [
                [row["entity"], row["entries"], row["receivable_eur"],
                 row["payable_eur"], row["net_eur"]]
                for row in model.intercompany_by_entity],
            [width * 0.2] + [width * 0.2] * 4, align_right=(1, 2, 3, 4)))

    story.append(Paragraph("Intercompany exceptions", s["h2"]))
    if model.intercompany_exceptions.rows:
        rows = [["Reference", "Month", "Payer", "Receiver", "Difference (EUR)", "Finding"]]
        for row in model.intercompany_exceptions.rows:
            rows.append([
                para(row.get("reference")), _text(row.get("month")),
                _text(row.get("paying_entity")), _text(row.get("receiving_entity")),
                fmt_amount(row.get("difference_eur")),
                para(f"{_text(row.get('check_status'))} - {_text(row.get('check_reason'))}"),
            ])
        story.append(_table(rows, [width * 0.22, width * 0.10, width * 0.09, width * 0.09,
                                   width * 0.14, width * 0.36], align_right=(4,)))
        if model.intercompany_exceptions.more_note:
            story.append(Paragraph(model.intercompany_exceptions.more_note, s["small"]))
    else:
        story.append(Paragraph("None. Every pair carries both sides and eliminates to zero.",
                               s["body"]))

    # --- FX ----------------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("FX source accuracy", s["h2"]))
    story.append(Paragraph(FX_NOTE, s["body"]))
    story.append(Spacer(1, 6))
    story.append(_table(
        [["Month", "Supplied rate", "Reference rate", "Difference", "Verdict"]] + [
            [row["month"], row["supplied"], row["reference"], row["difference"], row["verdict"]]
            for row in model.fx_summary],
        [width * 0.15, width * 0.18, width * 0.18, width * 0.15, width * 0.34],
        align_right=(1, 2, 3)))

    story.append(Paragraph("FX controls", s["h2"]))
    story.append(_table(
        [["Control", "Severity", "Status", "Finding"]] + [
            [para(f"{row.get('check_id')}<br/>{row.get('check_name')}"),
             _text(row.get("severity")), _text(row.get("status")), para(row.get("explanation"))]
            for row in model.fx_control_summary],
        [width * 0.20, width * 0.12, width * 0.10, width * 0.58]))

    # --- provenance --------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Provenance", s["h2"]))
    story.append(_table(
        [["Item", "Value"],
         ["Dataset label", model.dataset_label],
         ["Run timestamp (UTC)", model.run_timestamp],
         ["Controls version", model.controls_version],
         ["Report version", model.report_version],
         ["Close status", _text(model.decision.get("close_status"))],
         ["report_allowed", str(model.report_allowed).lower()]],
        [width * 0.35, width * 0.65]))

    if model.inputs:
        story.append(Paragraph("Input files", s["h2"]))
        story.append(_table(
            [["Role", "Path", "SHA-256"]] + [
                [para(item.get("role")), para(item.get("path")), para(item.get("sha256"))]
                for item in model.inputs],
            [width * 0.18, width * 0.42, width * 0.40]))

    if model.config_used:
        story.append(Paragraph("Control configuration", s["h2"]))
        story.append(_table(
            [["Parameter", "Value"]] + [
                [key, para(json.dumps(value, sort_keys=True) if isinstance(value, (dict, list))
                           else _text(value))]
                for key, value in sorted(model.config_used.items())],
            [width * 0.35, width * 0.65]))

    story.append(Spacer(1, 8))
    story.append(KeepTogether(Paragraph(NO_LLM_NOTE, s["small"])))
    return story


def render_pdf(model: ReportModel, path: str | Path) -> Path:
    """Write the PDF. The only function in this module that uses reportlab."""
    destination = assert_writable(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    margin = 18 * mm
    frame_width = A4[0] - 2 * margin
    document = BaseDocTemplate(
        str(destination),
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=24 * mm,
        title=f"Month-End Close {'Report' if model.report_allowed else 'Exception Report'} "
              f"- {model.dataset_label}",
        author="Month-End Close Assistant",
        subject=f"close_status={model.decision.get('close_status')}",
    )
    frame = Frame(margin, 24 * mm, frame_width, A4[1] - margin - 24 * mm, id="body")
    document.addPageTemplates([PageTemplate(id="main", frames=[frame])])

    footer_right = (
        f"{model.decision.get('close_status')}"
        if model.report_allowed
        else f"{model.decision.get('close_status')} - NOT APPROVED"
    )
    document.build(
        _story(model, frame_width),
        canvasmaker=lambda *args, **kwargs: _NumberedCanvas(
            *args,
            footer_left=f"{model.dataset_label}  |  {model.run_timestamp}",
            footer_right=footer_right,
            **kwargs,
        ),
    )
    return destination


# ---------------------------------------------------------------------------
# decision package
# ---------------------------------------------------------------------------
def write_decision_package(model: ReportModel, out_dir: str | Path = "out") -> dict[str, Path]:
    """Write the PDF, control results, decision and manifest into out/<label>/."""
    root = assert_writable(Path(out_dir) / slugify(model.dataset_label))
    root.mkdir(parents=True, exist_ok=True)

    stamp = re.sub(r"[^0-9A-Za-z]", "", model.run_timestamp) or "unstamped"
    pdf_path = root / f"{model.filename_stem}_{stamp}.pdf"
    csv_path = root / "control_results.csv"
    json_path = root / "close_decision.json"
    manifest_path = root / "package_manifest.json"

    render_pdf(model, pdf_path)
    pd.DataFrame(model.control_results_records).to_csv(assert_writable(csv_path), index=False)
    assert_writable(json_path).write_text(
        json.dumps(model.decision, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )

    outputs = {"report_pdf": pdf_path, "control_results": csv_path, "close_decision": json_path}
    manifest = {
        "dataset_label": model.dataset_label,
        "run_timestamp": model.run_timestamp,
        "close_status": model.decision.get("close_status"),
        "report_allowed": model.report_allowed,
        "controls_version": model.controls_version,
        "report_version": model.report_version,
        "hash_scope": (
            "SHA-256 is recorded for every input and for every generated output except "
            "this manifest, which cannot contain a stable hash of itself."
        ),
        "inputs": model.inputs,
        "outputs": [
            {"role": role, "path": str(path), "sha256": sha256_of(path)}
            for role, path in sorted(outputs.items())
        ],
    }
    assert_writable(manifest_path).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    outputs["package_manifest"] = manifest_path
    return outputs


def describe_inputs(paths: dict[str, str | Path]) -> list[dict]:
    """Role, path and SHA-256 for every input file, for the provenance appendix."""
    described: list[dict] = []
    for role, path in sorted(paths.items()):
        candidate = Path(path)
        if candidate.exists():
            described.append({"role": role, "path": str(candidate),
                              "sha256": sha256_of(candidate)})
    return described


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from controls import decide_close, run_controls  # noqa: E402
    from intercompany import check_elimination, generate_intercompany_entries  # noqa: E402
    from match import three_way_match  # noqa: E402

    parser = argparse.ArgumentParser(description="Produce the close report and decision package.")
    parser.add_argument("--data-dir", default="data/raw")
    parser.add_argument("--fx-rates", default=None, help="FX file under test (E4 run)")
    parser.add_argument("--fx-reference", default=None,
                        help="default data/raw/fx_rates_expected.csv")
    parser.add_argument("--ic-entries", default=None,
                        help="pre-generated intercompany entries (E5/E6 runs)")
    parser.add_argument("--dataset-label", default="clean")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--run-timestamp", default=None,
                        help="UTC timestamp; fixing it makes the output byte-identical")
    parser.add_argument("--max-exception-rows", type=int, default=25)
    parser.add_argument("--fail-on-blocked", dest="fail_on_blocked", action="store_true",
                        default=True)
    parser.add_argument("--no-fail-on-blocked", dest="fail_on_blocked", action="store_false")
    args = parser.parse_args()

    try:
        base = Path(args.data_dir)
        paths = {
            "purchase_orders": base / "purchase_orders.csv",
            "invoices": base / "invoices.csv",
            "payments": base / "payments.csv",
            "shared_costs": base / "shared_costs.csv",
            "fx_rates": Path(args.fx_rates) if args.fx_rates else base / "fx_rates.csv",
            "fx_reference": Path(args.fx_reference) if args.fx_reference
            else Path("data/raw/fx_rates_expected.csv"),
        }
        if args.ic_entries:
            paths["intercompany_entries"] = Path(args.ic_entries)

        read = lambda p: pd.read_csv(p, dtype=str)  # noqa: E731
        pos_df = read(paths["purchase_orders"])
        invoices_df = read(paths["invoices"])
        payments_df = read(paths["payments"])
        costs_df = read(paths["shared_costs"])
        fx_df = read(paths["fx_rates"])
        fx_reference_df = read(paths["fx_reference"])

        match_df = three_way_match(pos_df, invoices_df, payments_df)
        entries_df = (
            pd.read_csv(paths["intercompany_entries"])
            if args.ic_entries
            else generate_intercompany_entries(costs_df, fx_df)
        )
        elimination_df = check_elimination(entries_df)

        timestamp = args.run_timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
        results_df = run_controls(
            match_results=match_df,
            invoices=invoices_df,
            payments=payments_df,
            ic_entries=entries_df,
            ic_elimination=elimination_df,
            shared_costs=costs_df,
            fx_actual=fx_df,
            fx_reference=fx_reference_df,
            purchase_orders=pos_df,
            dataset_label=args.dataset_label,
            run_timestamp=timestamp,
        )
        decision = decide_close(results_df, dataset_label=args.dataset_label,
                                run_timestamp=timestamp)

        model = build_report_model(
            match_results=match_df,
            invoices=invoices_df,
            payments=payments_df,
            ic_entries=entries_df,
            ic_elimination=elimination_df,
            shared_costs=costs_df,
            fx_actual=fx_df,
            fx_reference=fx_reference_df,
            control_results=results_df,
            decision=decision,
            inputs=describe_inputs(paths),
            max_exception_rows=args.max_exception_rows,
        )
        written = write_decision_package(model, args.out_dir)

        print(f"\nDataset:        {model.dataset_label}")
        print(f"Close status:   {decision['close_status']}  (report_allowed="
              f"{str(model.report_allowed).lower()})")
        if decision["blocking_controls"]:
            print(f"Blocking:       {', '.join(decision['blocking_controls'])}")
        print(f"Document:       {written['report_pdf']}")
        print(f"Package:        {written['package_manifest'].parent}")

        sys.exit(2 if args.fail_on_blocked and not model.report_allowed else 0)
    except ProtectedPathError as exc:
        print(f"Refused: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"{type(exc).__name__}: {exc}")
        sys.exit(1)
