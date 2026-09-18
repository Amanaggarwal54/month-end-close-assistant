"""
Month-End Close Assistant - downstream error injection (E5 and E6).

Creates deliberately corrupted COPIES of a clean intercompany output so the
control layer can be proved to catch posting-level corruption. The clean
artefact is only ever read.

E5  delete exactly one side (the payable) of one cross-currency pair
    -> expected detection: ICO-01 (missing payable)

E6  blank the local_amount on one side of a different cross-currency pair
    -> expected detection: ICO-03 (accounting amounts complete)

E6 deliberately blanks the local amount rather than the EUR equivalent: the
elimination check compares EUR equivalents, so it still passes, and only the
completeness control catches the defect. That proves the two controls are
independent rather than redundant.

Target selection is deterministic and rule-based (first cross-currency pair in
sorted order, and the next one for E6), so no transaction id is hard-coded.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

MANIFEST_COLUMNS = [
    "error_id",
    "source_file",
    "output_file",
    "pair_id",
    "entry_id",
    "field_changed",
    "original_value",
    "new_value",
    "expected_control",
    "injected_at",
]

EXPECTED_CONTROL = {"E5": "ICO-01", "E6": "ICO-03"}
PROTECTED_DIRECTORY = "raw"


class ProtectedPathError(RuntimeError):
    """Raised when an output path would write into the clean dataset."""


def assert_writable(path: str | Path) -> Path:
    """Refuse any output path inside a data/raw directory.

    The guard is a hard failure rather than a warning: the value of the clean
    dataset is that it has never been touched by a test.
    """
    resolved = Path(path).resolve()
    parts = [part.lower() for part in resolved.parts]
    for index, part in enumerate(parts):
        if part == PROTECTED_DIRECTORY and index > 0 and parts[index - 1] == "data":
            raise ProtectedPathError(
                f"refusing to write to {resolved}: data/raw holds the clean dataset"
            )
    return resolved


def find_cross_currency_pairs(entries: pd.DataFrame) -> list[str]:
    """Pair ids whose two sides are booked in different currencies, sorted.

    Cross-currency pairs are the harder test case: the deleted or blanked value
    cannot be inferred by glancing at the counterparty row.
    """
    accounting = entries[entries["entry_type"].isin(["RECEIVABLE", "PAYABLE"])]
    currencies = accounting.groupby("pair_id")["currency"].nunique()
    return sorted(currencies[currencies > 1].index.astype(str))


def _select_targets(entries: pd.DataFrame) -> tuple[str, str]:
    """Deterministically choose distinct target pairs for E5 and E6."""
    candidates = find_cross_currency_pairs(entries)
    if len(candidates) < 2:
        raise ValueError(
            f"need at least two cross-currency pairs to plant E5 and E6, found {len(candidates)}"
        )
    return candidates[0], candidates[1]


def plant_e5(entries: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Delete the payable side of the first cross-currency pair."""
    target, _ = _select_targets(entries)
    victim = entries[(entries["pair_id"] == target) & (entries["entry_type"] == "PAYABLE")]
    if len(victim) != 1:
        raise ValueError(f"expected exactly one payable for {target}, found {len(victim)}")

    row = victim.iloc[0]
    corrupted = entries.drop(victim.index).reset_index(drop=True)
    record = {
        "error_id": "E5",
        "pair_id": target,
        "entry_id": row["entry_id"],
        "field_changed": "<entire row deleted>",
        "original_value": f"{row['entry_type']} {row['currency']} {row['local_amount']}",
        "new_value": "",
        "expected_control": EXPECTED_CONTROL["E5"],
    }
    return corrupted, record


def plant_e6(entries: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Blank the local_amount on the payable side of the second cross-currency pair."""
    _, target = _select_targets(entries)
    victim = entries[(entries["pair_id"] == target) & (entries["entry_type"] == "PAYABLE")]
    if len(victim) != 1:
        raise ValueError(f"expected exactly one payable for {target}, found {len(victim)}")

    index = victim.index[0]
    original = entries.loc[index, "local_amount"]
    corrupted = entries.copy()
    corrupted.loc[index, "local_amount"] = pd.NA
    record = {
        "error_id": "E6",
        "pair_id": target,
        "entry_id": entries.loc[index, "entry_id"],
        "field_changed": "local_amount",
        "original_value": original,
        "new_value": "",
        "expected_control": EXPECTED_CONTROL["E6"],
    }
    return corrupted.reset_index(drop=True), record


PLANTERS = {"E5": plant_e5, "E6": plant_e6}


def append_manifest(manifest_path: str | Path, record: dict) -> pd.DataFrame:
    """Append one injection record, replacing any earlier entry for the same error."""
    path = assert_writable(manifest_path)
    if path.exists():
        manifest = pd.read_csv(path, dtype=str)
        manifest = manifest[manifest["error_id"] != record["error_id"]]
    else:
        manifest = pd.DataFrame(columns=MANIFEST_COLUMNS)

    manifest = pd.concat(
        [manifest, pd.DataFrame([record], columns=MANIFEST_COLUMNS)], ignore_index=True
    ).sort_values("error_id", kind="mergesort")
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(path, index=False)
    return manifest

def _portable_manifest_path(path: str | Path, project_root: Path) -> str:
    """Return a repo-relative path when possible, otherwise a safe filename."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return resolved.name
    
def inject(
    error_id: str,
    source_path: str | Path,
    output_path: str | Path,
    manifest_path: str | Path | None = None,
) -> dict:
    """Read the clean entries, plant one error, write the corrupted copy.

    The source file is opened read-only and never rewritten.
    """
    if error_id not in PLANTERS:
        raise ValueError(f"unknown error_id {error_id}; expected one of {sorted(PLANTERS)}")

    destination = assert_writable(output_path)
    source = Path(source_path).resolve()
    entries = pd.read_csv(source)

    corrupted, record = PLANTERS[error_id](entries)
    destination.parent.mkdir(parents=True, exist_ok=True)
    corrupted.to_csv(destination, index=False)

    project_root = Path.cwd().resolve()

    record.update(
        {
            "source_file": _portable_manifest_path(source, project_root),
            "output_file": _portable_manifest_path(destination, project_root),
            "injected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    if manifest_path:
        append_manifest(manifest_path, record)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Plant downstream error E5 or E6.")
    parser.add_argument("--error", required=True, choices=sorted(PLANTERS))
    parser.add_argument("--source", default="data/raw/intercompany_entries.csv")
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest", default="data/error_data/downstream_planted_errors.csv")
    args = parser.parse_args()

    result = inject(args.error, args.source, args.out, args.manifest)
    print(f"\nPlanted {result['error_id']}")
    for key in ("pair_id", "entry_id", "field_changed", "original_value", "expected_control"):
        print(f"  {key}: {result[key]}")
    print(f"  output: {result['output_file']}")
    print(f"  manifest: {args.manifest}")
