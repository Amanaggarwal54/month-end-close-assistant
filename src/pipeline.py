"""
Month-End Close Assistant - Step 9: end-to-end pipeline.

One command runs the whole close:

    source data -> match -> intercompany -> controls -> decide_close -> report

This module is orchestration only. It contains no matching rule, no FX
conversion, no tolerance comparison, no severity logic and no formatting: every
step calls a function that already exists in ``match.py``, ``intercompany.py``,
``controls.py`` or ``report.py``. Its own code is limited to sequencing,
argument handling, the console summary and the exit code.

It is also the *only* orchestration implementation in the project. The CLI in
``report.py`` delegates here, so the two entry points cannot drift apart and the
package produced is identical whichever is used.

Layers, mirroring the split used by ``controls.py`` and ``report.py``:

    load_artefacts()   I/O: read the source CSVs
    execute_close()    pure: run every step, return typed results, write nothing
    write_outputs()    I/O: persist the decision package
    run_close()        load -> execute -> write
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from controls import ControlConfig, decide_close, run_controls  # noqa: E402
from intercompany import check_elimination, generate_intercompany_entries  # noqa: E402
from match import three_way_match  # noqa: E402
from plant_downstream_errors import ProtectedPathError  # noqa: E402
from report import ReportModel, build_report_model, describe_inputs, write_decision_package  # noqa: E402

PIPELINE_VERSION = "1.0.0"

EXIT_ALLOWED = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2

DEFAULT_FX_REFERENCE = Path("data/raw/fx_rates_expected.csv")


# ---------------------------------------------------------------------------
# configuration and results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CloseRunConfig:
    """Everything that varies between close runs."""

    data_dir: Path = Path("data/raw")
    fx_rates: Path | None = None
    fx_reference: Path | None = None
    ic_entries: Path | None = None
    dataset_label: str = "clean"
    out_dir: Path = Path("out")
    run_timestamp: str | None = None
    max_exception_rows: int = 25
    control_config: ControlConfig | None = None
    write_package: bool = True
    fail_on_blocked: bool = True

    def resolved_paths(self) -> dict[str, Path]:
        """Input paths by role, in the order the provenance appendix lists them.

        The FX reference deliberately defaults to data/raw regardless of
        --data-dir: a reference that lives inside the dataset under test would
        not be independent of it.
        """
        base = Path(self.data_dir)
        paths: dict[str, Path] = {
            "purchase_orders": base / "purchase_orders.csv",
            "invoices": base / "invoices.csv",
            "payments": base / "payments.csv",
            "shared_costs": base / "shared_costs.csv",
            "fx_rates": Path(self.fx_rates) if self.fx_rates else base / "fx_rates.csv",
            "fx_reference": Path(self.fx_reference) if self.fx_reference else DEFAULT_FX_REFERENCE,
        }
        if self.ic_entries:
            paths["intercompany_entries"] = Path(self.ic_entries)
        return paths


@dataclass(frozen=True)
class SourceFrames:
    """The raw inputs, exactly as read from disk."""

    purchase_orders: pd.DataFrame
    invoices: pd.DataFrame
    payments: pd.DataFrame
    shared_costs: pd.DataFrame
    fx_rates: pd.DataFrame
    fx_reference: pd.DataFrame
    intercompany_entries: pd.DataFrame | None
    paths: dict[str, Path]
    inputs: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CloseArtefacts:
    """What the accounting modules produced for this run."""

    match_results: pd.DataFrame
    ic_entries: pd.DataFrame
    ic_elimination: pd.DataFrame


@dataclass(frozen=True)
class CloseRunResult:
    config: CloseRunConfig
    artefacts: CloseArtefacts
    control_results: pd.DataFrame
    decision: dict
    model: ReportModel
    run_timestamp: str
    written: dict[str, Path] = field(default_factory=dict)

    @property
    def report_allowed(self) -> bool:
        return bool(self.decision.get("report_allowed"))

    @property
    def close_status(self) -> str:
        return str(self.decision.get("close_status"))

    @property
    def blocking_controls(self) -> list[str]:
        return list(self.decision.get("blocking_controls") or [])

    @property
    def exit_code(self) -> int:
        if self.report_allowed or not self.config.fail_on_blocked:
            return EXIT_ALLOWED
        return EXIT_BLOCKED


# ---------------------------------------------------------------------------
# 1. load
# ---------------------------------------------------------------------------
def load_artefacts(config: CloseRunConfig) -> SourceFrames:
    """Read every input file. The only reading this module does."""
    paths = config.resolved_paths()
    missing = [str(path) for path in paths.values() if not Path(path).exists()]
    if missing:
        raise FileNotFoundError(f"input file(s) not found: {', '.join(missing)}")

    read = lambda path: pd.read_csv(path, dtype=str)  # noqa: E731 - ids stay text
    entries = (
        pd.read_csv(paths["intercompany_entries"]) if "intercompany_entries" in paths else None
    )
    return SourceFrames(
        purchase_orders=read(paths["purchase_orders"]),
        invoices=read(paths["invoices"]),
        payments=read(paths["payments"]),
        shared_costs=read(paths["shared_costs"]),
        fx_rates=read(paths["fx_rates"]),
        fx_reference=read(paths["fx_reference"]),
        intercompany_entries=entries,
        paths=paths,
        inputs=describe_inputs(paths),
    )


# ---------------------------------------------------------------------------
# 2. execute
# ---------------------------------------------------------------------------
def execute_close(sources: SourceFrames, config: CloseRunConfig) -> CloseRunResult:
    """Run every step and return the results. Pure: no file I/O, no mutation.

    The timestamp is resolved once here and passed to the controls, the decision
    and the report, so a single run can never carry two different times.
    """
    timestamp = config.run_timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")

    match_results = three_way_match(
        sources.purchase_orders, sources.invoices, sources.payments
    )
    entries = (
        sources.intercompany_entries
        if sources.intercompany_entries is not None
        else generate_intercompany_entries(sources.shared_costs, sources.fx_rates)
    )
    elimination = check_elimination(entries)

    control_results = run_controls(
        match_results=match_results,
        invoices=sources.invoices,
        payments=sources.payments,
        ic_entries=entries,
        ic_elimination=elimination,
        shared_costs=sources.shared_costs,
        fx_actual=sources.fx_rates,
        fx_reference=sources.fx_reference,
        purchase_orders=sources.purchase_orders,
        config=config.control_config,
        dataset_label=config.dataset_label,
        run_timestamp=timestamp,
    )
    decision = decide_close(
        control_results,
        config=config.control_config,
        dataset_label=config.dataset_label,
        run_timestamp=timestamp,
    )
    model = build_report_model(
        match_results=match_results,
        invoices=sources.invoices,
        payments=sources.payments,
        ic_entries=entries,
        ic_elimination=elimination,
        shared_costs=sources.shared_costs,
        fx_actual=sources.fx_rates,
        fx_reference=sources.fx_reference,
        control_results=control_results,
        decision=decision,
        inputs=sources.inputs,
        max_exception_rows=config.max_exception_rows,
    )
    return CloseRunResult(
        config=config,
        artefacts=CloseArtefacts(match_results, entries, elimination),
        control_results=control_results,
        decision=decision,
        model=model,
        run_timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# 3. write
# ---------------------------------------------------------------------------
def write_outputs(result: CloseRunResult) -> CloseRunResult:
    """Persist the decision package. Writing is delegated to report.py, which
    carries the data/raw guard, so this module adds no write path of its own."""
    if not result.config.write_package:
        return result
    written = write_decision_package(result.model, result.config.out_dir)
    return replace(result, written=written)


def run_close(config: CloseRunConfig) -> CloseRunResult:
    """Load, execute, write. The single entry point both CLIs call."""
    return write_outputs(execute_close(load_artefacts(config), config))


# ---------------------------------------------------------------------------
# console
# ---------------------------------------------------------------------------
def summarise(result: CloseRunResult) -> str:
    """The five-line terminal summary, plus the blocking findings when blocked."""
    lines = [
        "",
        f"Dataset:        {result.config.dataset_label}",
        f"Close status:   {result.close_status}  "
        f"(report_allowed={str(result.report_allowed).lower()})",
    ]
    if result.blocking_controls:
        lines.append(f"Blocking:       {', '.join(result.blocking_controls)}")
        for row in result.model.blocking_explanations:
            lines.append(f"  - {row.get('check_id')}: {row.get('explanation')}")
    if result.written:
        lines.append(f"Document:       {result.written['report_pdf']}")
        lines.append(f"Package:        {result.written['report_pdf'].parent}")
    else:
        lines.append("Dry run:        no files written")
    return "\n".join(lines)


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the month-end close end to end and produce the decision package."
    )
    parser.add_argument("--data-dir", default="data/raw")
    parser.add_argument("--fx-rates", default=None, help="FX file under test (E4 run)")
    parser.add_argument("--fx-reference", default=None,
                        help=f"reference FX file, default {DEFAULT_FX_REFERENCE}")
    parser.add_argument("--ic-entries", default=None,
                        help="pre-generated intercompany entries (E5/E6 runs)")
    parser.add_argument("--dataset-label", default="clean")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--run-timestamp", default=None,
                        help="UTC timestamp; fixing it makes the package byte-identical")
    parser.add_argument("--max-exception-rows", type=int, default=25)
    parser.add_argument("--fail-on-blocked", dest="fail_on_blocked", action="store_true",
                        default=True)
    parser.add_argument("--no-fail-on-blocked", dest="fail_on_blocked", action="store_false")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute the decision and print it, write nothing")
    parser.add_argument("--quiet", action="store_true")
    return parser


def config_from_args(args) -> CloseRunConfig:
    return CloseRunConfig(
        data_dir=Path(args.data_dir),
        fx_rates=Path(args.fx_rates) if args.fx_rates else None,
        fx_reference=Path(args.fx_reference) if args.fx_reference else None,
        ic_entries=Path(args.ic_entries) if args.ic_entries else None,
        dataset_label=args.dataset_label,
        out_dir=Path(args.out_dir),
        run_timestamp=args.run_timestamp,
        max_exception_rows=args.max_exception_rows,
        write_package=not args.dry_run,
        fail_on_blocked=args.fail_on_blocked,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the exit code rather than calling sys.exit,
    so tests can assert on it directly."""
    args = build_parser().parse_args(argv)
    try:
        result = run_close(config_from_args(args))
    except ProtectedPathError as exc:
        print(f"Refused: {exc}")
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"Input error: {exc}")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report, never traceback
        print(f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR

    if not args.quiet:
        print(summarise(result))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
