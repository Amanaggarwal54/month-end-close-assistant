# Step 13C — Claude investigation in the review workspace

Step 13A defines the advisory boundary and validates every provider result.
Step 13B puts a real Claude provider behind that boundary. Step 13C connects
that provider to the post-close review workspace.

## Flow

```text
immutable close package
        |
review/<dataset>/exception_resolution.json
        |
selected exception
        |
control_results.csv (verified against package manifest)
        |
build_investigation_packet()
        |
ClaudeInvestigationProvider
        |
13A validate_investigation_result()
        |
review/<dataset>/investigation_advice.json
        |
human reviewer
```

The investigation file is deliberately separate from the close package. A
stored advisory cannot change the close decision, control results,
`exception_register.json`, or the exception workflow status.

## Stored record

Each record carries:

- a deterministic `investigation_id`
- the selected `exception_id`
- the caller-supplied `occurred_at`
- the SHA-256 of the review workspace seen by the provider
- the close-package provenance copied from the workspace
- the validated advisory, including provider/model/machine-generated metadata

The ID is derived from the dataset, exception, timestamp, workspace hash and
validated advisory. Re-running the same advisory with the same inputs replaces
that record instead of creating duplicates; different advice or a different
caller-supplied timestamp creates a distinct record.

## Evidence boundary

Immediately before a provider call, Step 13C re-verifies the immutable close
package against its manifest. It reads the package's `control_results.csv`
with the standard library and passes only the rows belonging to the selected
control into the 13A packet builder. Optional source-file references can also
be supplied by the caller; Step 13C does not read those files behind the
provider's back.

The provider still sees the exception details carried by the review workspace,
and 13A still rejects invented evidence or any decision field.

## CLI

The existing reviewer CLI gains one command:

```text
python src/review_cli.py investigate \
  --dataset-label E4 \
  --review-dir review \
  --exception-id EXC-FXC-03-E4 \
  --occurred-at "2026-04-03T10:00:00Z"
```

The command requires the caller to supply the timestamp. Credentials remain
outside the command line and are read by `ClaudeInvestigationProvider` from
`ANTHROPIC_API_KEY`.

Optional flags are `--model`, `--max-tokens`, `--structured-output-mode`, and
repeatable `--source-file` references.

## Failure properties

A provider error produces no advice file. A result that fails 13A validation
produces no advice file. A tampered close package is rejected before the model
is called. A review workspace write targeting the close-package directory is
rejected.

Most importantly, investigation is still advisory: storing, viewing or
resolving an advisory record does not alter the deterministic close decision.
