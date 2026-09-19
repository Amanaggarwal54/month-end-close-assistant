from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

DEMO_CLEAN_LABEL = "demo-clean"
DEMO_E4_LABEL = "demo-E4"
DEMO_E4_DIR = ROOT / "out" / "demo-e4"
REVIEW_DIR = ROOT / "review"
DEMO_TIMESTAMP = "2026-09-19T10:00:00Z"


def run(command: list[str]) -> None:
    print()
    print("=" * 72)
    print("$ " + " ".join(command))
    print("=" * 72)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the recruiter-ready month-end close demonstration."
    )
    parser.add_argument(
        "--with-ai",
        action="store_true",
        help="also run the Gemini investigation for the E4 exception",
    )
    args = parser.parse_args()

    print("MONTH-END CLOSE ASSISTANT — DEMO")
    print("---------------------------------")

    # 1. Clean close
    run([
        PYTHON,
        "-m",
        "src.pipeline",
        "--dataset-label",
        DEMO_CLEAN_LABEL,
    ])

    # 2. Deliberately broken FX close
    run([
        PYTHON,
        "-m",
        "src.pipeline",
        "--dataset-label",
        DEMO_E4_LABEL,
        "--fx-rates",
        str(ROOT / "data" / "error_data" / "fx_rates.csv"),
        "--fx-reference",
        str(ROOT / "data" / "error_data" / "fx_rates_expected.csv"),
        "--no-fail-on-blocked",
    ])

    # 3. Build the reviewer workspace from the immutable E4 package.
    run([
        PYTHON,
        "-m",
        "src.review_cli",
        "create",
        "--package-dir",
        str(DEMO_E4_DIR),
        "--review-dir",
        str(REVIEW_DIR),
        "--force",
    ])

    # 4. Optional AI investigation.
    if args.with_ai:
        if not os.environ.get("GEMINI_API_KEY"):
            print()
            print("Gemini investigation skipped: GEMINI_API_KEY is not set.")
        else:
            try:
                run([
                    PYTHON,
                    "-m",
                    "src.review_cli",
                    "investigate",
                    "--dataset-label",
                    DEMO_E4_LABEL,
                    "--review-dir",
                    "review",
                    "--exception-id",
                    "EXC-FXC-03-E4",
                    "--occurred-at",
                    DEMO_TIMESTAMP,
                    "--provider",
                    "gemini",
                    "--source-file",
                    "data/raw/fx_rates_expected.csv",
                ])
            except subprocess.CalledProcessError as error:
                print()
                print(
                    "Gemini investigation unavailable "
                    f"(exit code {error.returncode}). "
                    "The deterministic close demo still completed successfully."
                )
    else:
        print()
        print("Gemini investigation skipped.")
        print("Run with --with-ai to include the AI investigation step.")

    print()
    print("=" * 72)
    print("DEMO COMPLETE")
    print("=" * 72)
    print("Clean package : out/demo-clean/")
    print("Blocked E4    : out/demo-e4/")
    print("Review        : review/demo-e4/")
    print()

    advice = REVIEW_DIR / "demo-e4" / "investigation_advice.json"
    if advice.exists():
        print(f"AI advisory  : {advice}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
