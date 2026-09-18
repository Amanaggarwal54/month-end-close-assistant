"""
Month-End Close Assistant - Step 12E: the reviewer command line.

A thin front end over ``review_workspace.py``. It parses arguments, loads a
workspace, applies exactly one operation, writes it back, and prints something a
person can read. It decides nothing.

Every lifecycle rule - which transitions are allowed, that RESOLVED is terminal,
that a resolution needs a comment and evidence - lives in
``exception_workflow.py`` and reaches this module only through
``review_workspace.py``. A rule enforced here as well as there could drift out of
step with it, so none are.

The close package is never written to. A resolved exception changes the review
record and nothing else: the close decision, the control results, the audit
trail and the immutable ``exception_register.json`` are all left exactly as the
close produced them.

No clock. Every mutating command requires ``--occurred-at`` from the caller and
stores it verbatim.

Commands
--------
    create      build a workspace from an immutable close package
    list        one line per exception
    show        the complete record for one exception
    assign      set or clear the owner
    transition  move an exception to a new status
    resolve     transition to RESOLVED, with comment and evidence

Exit codes
----------
    0  the command succeeded
    1  the input could not be used (missing workspace, unknown exception, bad argument)
    2  the workflow refused the operation (invalid transition, missing comment or evidence)
    3  the close package does not match its own manifest
    4  the command would have written inside the immutable close package
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from exception_workflow import INVESTIGATING, OPEN, RESOLVED  # noqa: E402
from review_workspace import (  # noqa: E402
    WORKSPACE_FILENAME,
    PackageIntegrityError,
    WorkspaceWriteError,
    create_resolution_workspace,
    get_review_exception,
    load_workspace,
    review_assign_owner,
    review_transition_exception,
    workspace_path_for,
    write_workspace,
)

__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_LIFECYCLE",
    "EXIT_INTEGRITY",
    "EXIT_PROTECTED",
    "build_parser",
    "main",
]

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_LIFECYCLE = 2
EXIT_INTEGRITY = 3
EXIT_PROTECTED = 4

#: Statuses a reviewer may name on the command line. RESOLVED is reachable
#: through `transition` too, but `resolve` is the command that asks for the
#: comment and evidence it requires.
SELECTABLE_STATUSES = (OPEN, INVESTIGATING, RESOLVED)

EVIDENCE_SEPARATOR = ":"


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------
def _evidence_item(value: str) -> dict[str, str]:
    """Parse ``type:reference`` into the evidence shape the workflow stores.

    The reference may itself contain colons - a path or a timestamp often does -
    so only the first one separates the two fields.
    """
    if EVIDENCE_SEPARATOR not in value:
        raise argparse.ArgumentTypeError(
            f"evidence must be given as type{EVIDENCE_SEPARATOR}reference, "
            f"for example control_result{EVIDENCE_SEPARATOR}FXC-03; got {value!r}"
        )

    kind, reference = value.split(EVIDENCE_SEPARATOR, 1)
    kind, reference = kind.strip(), reference.strip()

    if not kind or not reference:
        raise argparse.ArgumentTypeError(
            f"evidence needs both a type and a reference; got {value!r}"
        )

    return {"type": kind, "reference": reference}


def _add_workspace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset-label", required=True,
                        help="the close the workspace belongs to, e.g. E4")
    parser.add_argument("--review-dir", default="review",
                        help="root of the review tree (default: review)")


def _add_exception_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exception-id", required=True,
                        help="the workflow id, e.g. EXC-FXC-03-E4")


def _add_occurred_at(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--occurred-at",
        required=True,
        help="when this happened, supplied by you and stored verbatim; "
             "nothing here reads a clock",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_cli",
        description="Review and resolve exceptions raised by a month-end close. "
                    "The close package itself is never modified.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "create", help="create a review workspace from an immutable close package"
    )
    create.add_argument("--package-dir", required=True,
                        help="the close package, e.g. out/e4")
    create.add_argument("--review-dir", default="review",
                        help="root of the review tree (default: review)")
    create.add_argument(
        "--force", action="store_true",
        help="overwrite an existing workspace, discarding any review progress in it",
    )

    listing = commands.add_parser("list", help="list the exceptions in a workspace")
    _add_workspace_arguments(listing)

    show = commands.add_parser("show", help="show one exception in full")
    _add_workspace_arguments(show)
    _add_exception_argument(show)

    assign = commands.add_parser("assign", help="set or clear the owner")
    _add_workspace_arguments(assign)
    _add_exception_argument(assign)
    owner_group = assign.add_mutually_exclusive_group(required=True)
    owner_group.add_argument("--owner", help="who owns the investigation")
    owner_group.add_argument("--clear-owner", action="store_true",
                             help="remove the current owner")
    assign.add_argument("--comment", help="optional note recorded with the change")
    _add_occurred_at(assign)

    transition = commands.add_parser("transition", help="move an exception to a new status")
    _add_workspace_arguments(transition)
    _add_exception_argument(transition)
    transition.add_argument("--status", required=True, choices=SELECTABLE_STATUSES,
                            help="the status to move to")
    transition.add_argument("--comment", help="why; required to reach RESOLVED")
    transition.add_argument("--evidence", action="append", type=_evidence_item,
                            metavar="TYPE:REFERENCE",
                            help="repeatable; required to reach RESOLVED")
    _add_occurred_at(transition)

    resolve = commands.add_parser(
        "resolve", help="resolve an exception, with comment and evidence"
    )
    _add_workspace_arguments(resolve)
    _add_exception_argument(resolve)
    resolve.add_argument("--comment", required=True,
                         help="what was concluded")
    resolve.add_argument("--evidence", action="append", type=_evidence_item,
                         required=True, metavar="TYPE:REFERENCE",
                         help="repeatable; at least one reference is required")
    _add_occurred_at(resolve)

    return parser


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def _dump(payload: Any) -> str:
    """The project's deterministic JSON convention."""
    return json.dumps(payload, indent=2, sort_keys=True)


def _format_listing(workspace: dict[str, Any]) -> str:
    """One line per exception, in the workspace's own deterministic order."""
    exceptions = workspace.get("exceptions") or []
    source = workspace.get("source_package") or {}

    lines = [
        f"Dataset:     {workspace.get('dataset_label')}",
        f"Package:     {source.get('package_path')}",
        f"Close:       {source.get('close_status')}  "
        f"(report_allowed={str(source.get('report_allowed')).lower()})",
        f"Exceptions:  {len(exceptions)}",
    ]

    if not exceptions:
        lines.append("")
        lines.append("No exceptions were raised by this close.")
        return "\n".join(lines)

    rows = [
        (
            str(entry.get("exception_id") or ""),
            str(entry.get("status") or ""),
            str(entry.get("owner") or "-"),
            str(entry.get("severity") or ""),
            str(entry.get("control_id") or ""),
        )
        for entry in exceptions
    ]
    headers = ("EXCEPTION", "STATUS", "OWNER", "SEVERITY", "CONTROL")
    widths = [
        max(len(headers[index]), max(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    lines.append("")
    lines.append("  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(row))).rstrip())

    return "\n".join(lines)


def _describe_change(exception: dict[str, Any]) -> str:
    event = (exception.get("history") or [])[-1]
    return (
        f"{exception.get('exception_id')}: {event.get('action')} "
        f"-> status {exception.get('status')}, owner {exception.get('owner') or '-'} "
        f"at {event.get('occurred_at')}"
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def _command_create(args: argparse.Namespace, out) -> int:
    label_probe = Path(args.package_dir).name
    existing = workspace_path_for(args.review_dir, label_probe)

    # Re-creating rebuilds every entry from the close package, which would throw
    # away an investigation already recorded here. Refuse unless asked twice.
    if existing.exists() and not args.force:
        print(
            f"A workspace already exists at {existing}. Re-creating it would "
            "discard the review progress it holds. Pass --force to overwrite it.",
            file=out,
        )
        return EXIT_USAGE

    workspace = create_resolution_workspace(args.package_dir, args.review_dir)
    written = workspace_path_for(args.review_dir, workspace["dataset_label"])

    print(f"Created {written}", file=out)
    print(
        f"  from package        {workspace['source_package']['package_path']}",
        file=out,
    )
    print(
        f"  register sha256     {workspace['source_package']['exception_register_sha256']}",
        file=out,
    )
    print(
        f"  manifest sha256     {workspace['source_package']['package_manifest_sha256']}",
        file=out,
    )
    print(f"  exceptions          {workspace['exception_count']}", file=out)
    return EXIT_OK


def _command_list(args: argparse.Namespace, out) -> int:
    workspace = load_workspace(args.review_dir, args.dataset_label)
    print(_format_listing(workspace), file=out)
    return EXIT_OK


def _command_show(args: argparse.Namespace, out) -> int:
    workspace = load_workspace(args.review_dir, args.dataset_label)
    print(_dump(get_review_exception(workspace, args.exception_id)), file=out)
    return EXIT_OK


def _apply(args: argparse.Namespace, out, operation) -> int:
    """Load, apply exactly one operation, persist, report."""
    workspace = load_workspace(args.review_dir, args.dataset_label)
    updated = operation(workspace)
    written = write_workspace(updated, args.review_dir)

    print(_describe_change(get_review_exception(updated, args.exception_id)), file=out)
    print(f"Updated {written}", file=out)
    return EXIT_OK


def _command_assign(args: argparse.Namespace, out) -> int:
    owner = None if args.clear_owner else args.owner
    return _apply(
        args,
        out,
        lambda workspace: review_assign_owner(
            workspace,
            args.exception_id,
            owner,
            comment=args.comment,
            occurred_at=args.occurred_at,
        ),
    )


def _command_transition(args: argparse.Namespace, out) -> int:
    return _apply(
        args,
        out,
        lambda workspace: review_transition_exception(
            workspace,
            args.exception_id,
            args.status,
            comment=args.comment,
            evidence=args.evidence,
            occurred_at=args.occurred_at,
        ),
    )


def _command_resolve(args: argparse.Namespace, out) -> int:
    # Resolution requirements are the workflow's, not this module's: the comment
    # and evidence are handed over and it decides whether they are sufficient.
    return _apply(
        args,
        out,
        lambda workspace: review_transition_exception(
            workspace,
            args.exception_id,
            RESOLVED,
            comment=args.comment,
            evidence=args.evidence,
            occurred_at=args.occurred_at,
        ),
    )


_COMMANDS = {
    "create": _command_create,
    "list": _command_list,
    "show": _command_show,
    "assign": _command_assign,
    "transition": _command_transition,
    "resolve": _command_resolve,
}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None, out=None) -> int:
    """Run one command and return its exit code.

    Returns rather than exits, so a test can assert on the code directly.
    """
    stream = out or sys.stdout
    args = build_parser().parse_args(argv)

    try:
        return _COMMANDS[args.command](args, stream)
    except WorkspaceWriteError as error:
        print(f"Refused: {error}", file=stream)
        return EXIT_PROTECTED
    except PackageIntegrityError as error:
        print(f"Package integrity check failed: {error}", file=stream)
        return EXIT_INTEGRITY
    except FileNotFoundError as error:
        print(f"Not found: {error}", file=stream)
        return EXIT_USAGE
    except KeyError as error:
        # exception_workflow raises KeyError for an unknown exception_id
        print(f"Not found: {error.args[0] if error.args else error}", file=stream)
        return EXIT_USAGE
    except ValueError as error:
        # the workflow refused the operation, or an argument was unusable
        print(f"Rejected: {error}", file=stream)
        return EXIT_LIFECYCLE
    except TypeError as error:
        print(f"Rejected: {error}", file=stream)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
