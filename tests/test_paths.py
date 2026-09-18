"""Step 12F: the reviewer tool no longer drags the reporting stack behind it.

``sha256_of``, ``slugify`` and ``display_path`` used to live in ``report.py``,
so anything needing one of them imported pandas, numpy and reportlab too. They
now live in ``paths.py``, which imports nothing but the standard library.

The tests here prove three things: the helpers behave exactly as before, there
is still only one implementation of each, and importing the reviewer tool no
longer loads the analytics stack. The last one is checked in a fresh subprocess,
because by the time this test module runs, the test session has already imported
pandas for its own reasons - asserting against this process's ``sys.modules``
would pass whatever the production imports actually do.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import paths  # noqa: E402
import report  # noqa: E402
import review_workspace  # noqa: E402
from paths import display_path, sha256_of, slugify  # noqa: E402

HEAVY_MODULES = ("pandas", "numpy", "reportlab")

FORBIDDEN_IN_PATHS = (
    "pandas", "numpy", "reportlab", "report", "pipeline", "controls",
    "match", "intercompany", "exception_workflow", "review_workspace",
    "review_cli", "audit", "plant_downstream_errors",
)


def _imported_modules(source_path: Path) -> set[str]:
    """Top-level module names imported by a source file, read statically."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def _import_in_subprocess(module_name: str) -> set[str]:
    """Import one module in a fresh interpreter and report what it loaded.

    A clean process is the only honest way to measure this: the test session
    itself has already imported pandas, so anything checked in-process would
    report modules this import did not cause.
    """
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        f"import {module_name}\n"
        "print(','.join(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, (
        f"importing {module_name} failed:\n{completed.stderr}"
    )
    return set(completed.stdout.strip().split(","))


# ---------------------------------------------------------------------------
# 1-2. the reviewer tool imports nothing heavy
# ---------------------------------------------------------------------------
def test_importing_review_workspace_does_not_load_the_reporting_stack():
    loaded = _import_in_subprocess("review_workspace")

    assert "review_workspace" in loaded, "the module under test must have imported"
    for heavy in HEAVY_MODULES:
        assert heavy not in loaded, f"{heavy} should not be loaded by review_workspace"
    assert "report" not in loaded


def test_importing_review_cli_does_not_load_the_reporting_stack():
    loaded = _import_in_subprocess("review_cli")

    assert "review_cli" in loaded
    assert "review_workspace" in loaded
    assert "exception_workflow" in loaded
    for heavy in HEAVY_MODULES:
        assert heavy not in loaded, f"{heavy} should not be loaded by review_cli"
    assert "report" not in loaded


def test_the_reporting_stack_is_still_loaded_where_it_belongs():
    """A negative test is only worth something if the positive one holds too."""
    loaded = _import_in_subprocess("report")
    for heavy in HEAVY_MODULES:
        assert heavy in loaded, f"report.py genuinely needs {heavy}"
    assert "paths" in loaded


def test_importing_paths_alone_loads_nothing_heavy():
    loaded = _import_in_subprocess("paths")
    for heavy in HEAVY_MODULES:
        assert heavy not in loaded


# ---------------------------------------------------------------------------
# 3-4. source-level guards
# ---------------------------------------------------------------------------
def test_review_workspace_does_not_import_report():
    imported = _imported_modules(SRC / "review_workspace.py")
    assert "report" not in imported
    assert "paths" in imported
    for heavy in HEAVY_MODULES:
        assert heavy not in imported


def test_review_cli_does_not_import_report():
    imported = _imported_modules(SRC / "review_cli.py")
    assert "report" not in imported
    for heavy in HEAVY_MODULES:
        assert heavy not in imported


def test_paths_imports_only_the_standard_library():
    imported = _imported_modules(SRC / "paths.py")

    for forbidden in FORBIDDEN_IN_PATHS:
        assert forbidden not in imported, f"paths.py must not import {forbidden}"

    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    unexpected = imported - stdlib - {"__future__"}
    assert not unexpected, f"paths.py imports non-stdlib modules: {sorted(unexpected)}"


def test_report_still_imports_the_helpers_rather_than_redefining_them():
    """One implementation, re-exported - not a copy on each side."""
    tree = ast.parse((SRC / "report.py").read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for helper in ("sha256_of", "slugify", "display_path"):
        assert helper not in defined, f"{helper} must not be redefined in report.py"

    assert "paths" in _imported_modules(SRC / "report.py")


# ---------------------------------------------------------------------------
# the helpers are the same objects, so they cannot diverge
# ---------------------------------------------------------------------------
def test_report_re_exports_the_very_same_functions():
    assert report.sha256_of is paths.sha256_of
    assert report.slugify is paths.slugify
    assert report.display_path is paths.display_path


def test_review_workspace_uses_the_very_same_functions():
    assert review_workspace.sha256_of is paths.sha256_of
    assert review_workspace.slugify is paths.slugify
    assert review_workspace.display_path is paths.display_path


def test_the_public_names_report_used_to_offer_still_import(tmp_path: Path):
    """Existing callers do `from report import sha256_of`; that must keep working."""
    from report import display_path as report_display_path
    from report import sha256_of as report_sha256_of
    from report import slugify as report_slugify

    sample = tmp_path / "sample.txt"
    sample.write_text("month-end", encoding="utf-8")

    assert report_sha256_of(sample) == sha256_of(sample)
    assert report_slugify("E1-E3") == slugify("E1-E3")
    assert report_display_path(sample) == display_path(sample)


# ---------------------------------------------------------------------------
# behaviour is unchanged
# ---------------------------------------------------------------------------
def test_sha256_of_matches_an_independent_hash(tmp_path: Path):
    """Computed a second way, so a broken move cannot pass by agreeing with itself."""
    sample = tmp_path / "artefact.bin"
    payload = b"close package bytes\n" * 10_000  # larger than the 64 KiB read chunk
    sample.write_bytes(payload)

    assert sha256_of(sample) == hashlib.sha256(payload).hexdigest()
    assert len(sha256_of(sample)) == 64


def test_sha256_of_handles_an_empty_file(tmp_path: Path):
    sample = tmp_path / "empty.txt"
    sample.write_bytes(b"")
    assert sha256_of(sample) == hashlib.sha256(b"").hexdigest()


def test_sha256_of_accepts_a_string_path(tmp_path: Path):
    sample = tmp_path / "artefact.txt"
    sample.write_text("x", encoding="utf-8")
    assert sha256_of(str(sample)) == sha256_of(sample)


def test_slugify_behaviour_is_unchanged():
    cases = {
        "E4": "e4",
        "E1-E3": "e1-e3",
        "clean": "clean",
        "Close 2026-03": "close-2026-03",
        "  spaced  out  ": "spaced-out",
        "../escape": "escape",
        "///": "unlabelled",
        "": "unlabelled",
        "MiXeD_Case!!": "mixed-case",
    }
    for label, expected in cases.items():
        assert slugify(label) == expected, label


def test_slugify_never_lets_a_label_escape_its_directory():
    for hostile in ("../../etc", "a/b/c", "..\\windows", "/absolute"):
        slug = slugify(hostile)
        assert "/" not in slug and "\\" not in slug and ".." not in slug


def test_display_path_is_relative_inside_the_working_directory():
    inside = Path.cwd() / "src" / "paths.py"
    assert display_path(inside) == "src/paths.py"
    assert not Path(display_path(inside)).is_absolute()


def test_display_path_is_absolute_and_posix_outside_the_working_directory(
    tmp_path: Path,
):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")

    rendered = display_path(outside)
    assert Path(rendered).is_absolute()
    assert "\\" not in rendered, "paths are rendered posix-style for portability"


def test_display_path_agrees_for_a_relative_and_an_absolute_spelling():
    """The reason the helper exists: one file, one rendering."""
    assert display_path("src/paths.py") == display_path(Path.cwd() / "src" / "paths.py")


# ---------------------------------------------------------------------------
# the refactor changed no behaviour anyone can observe
# ---------------------------------------------------------------------------
def test_a_close_package_is_still_byte_identical_across_two_runs(tmp_path: Path):
    """Determinism has to survive the move, since display_path shapes the PDF."""
    from pipeline import CloseRunConfig, run_close

    raw = ROOT / "data" / "raw"
    if not (raw / "invoices.csv").exists():
        pytest.skip("dataset not available")

    def close(out_dir: Path):
        return run_close(
            CloseRunConfig(
                data_dir=raw,
                out_dir=out_dir,
                run_timestamp="2026-03-31T18:00:00Z",
                dataset_label="clean",
            )
        ).written

    first = close(tmp_path / "first")
    second = close(tmp_path / "second")

    for role, path in first.items():
        if role == "package_manifest":
            continue  # records its own directory, which differs between runs
        assert sha256_of(path) == sha256_of(second[role]), role


def test_the_reviewer_workflow_still_works_end_to_end(tmp_path: Path):
    """The whole point of the move: the reviewer path must be unaffected."""
    import io

    from pipeline import CloseRunConfig, run_close
    from review_cli import EXIT_OK, main

    raw = ROOT / "data" / "raw"
    err = ROOT / "data" / "error_data"
    if not (err / "fx_rates.csv").exists():
        pytest.skip("dataset not available")

    package = run_close(
        CloseRunConfig(
            data_dir=raw,
            fx_rates=err / "fx_rates.csv",
            out_dir=tmp_path / "out",
            run_timestamp="2026-03-31T18:00:00Z",
            dataset_label="E4",
        )
    ).written["report_pdf"].parent

    before = {item.name: sha256_of(item) for item in sorted(package.iterdir())}

    def cli(*argv: str) -> int:
        return main(list(argv), out=io.StringIO())

    review = tmp_path / "review"
    assert cli("create", "--package-dir", str(package), "--review-dir", str(review)) == EXIT_OK
    assert cli("assign", "--dataset-label", "E4", "--review-dir", str(review),
               "--exception-id", "EXC-FXC-03-E4", "--owner", "finance.manager",
               "--occurred-at", "2026-04-01T09:00:00Z") == EXIT_OK
    assert cli("transition", "--dataset-label", "E4", "--review-dir", str(review),
               "--exception-id", "EXC-FXC-03-E4", "--status", "INVESTIGATING",
               "--comment", "Reviewing.", "--occurred-at", "2026-04-01T09:30:00Z") == EXIT_OK
    assert cli("resolve", "--dataset-label", "E4", "--review-dir", str(review),
               "--exception-id", "EXC-FXC-03-E4", "--comment", "Explained.",
               "--evidence", "control_result:FXC-03",
               "--occurred-at", "2026-04-02T14:15:00Z") == EXIT_OK

    after = {item.name: sha256_of(item) for item in sorted(package.iterdir())}
    assert after == before


def test_workspace_provenance_still_records_the_package_hashes(tmp_path: Path):
    from pipeline import CloseRunConfig, run_close
    from review_workspace import (
        MANIFEST_FILENAME,
        REGISTER_FILENAME,
        create_resolution_workspace,
    )

    raw = ROOT / "data" / "raw"
    err = ROOT / "data" / "error_data"
    if not (err / "fx_rates.csv").exists():
        pytest.skip("dataset not available")

    package = run_close(
        CloseRunConfig(
            data_dir=raw,
            fx_rates=err / "fx_rates.csv",
            out_dir=tmp_path / "out",
            run_timestamp="2026-03-31T18:00:00Z",
            dataset_label="E4",
        )
    ).written["report_pdf"].parent

    workspace = create_resolution_workspace(package, tmp_path / "review")
    source = workspace["source_package"]

    assert source["exception_register_sha256"] == sha256_of(package / REGISTER_FILENAME)
    assert source["package_manifest_sha256"] == sha256_of(package / MANIFEST_FILENAME)
