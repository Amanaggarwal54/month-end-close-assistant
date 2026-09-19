# System Architecture

## Overview

The Month-End Close Assistant separates deterministic finance processing from post-close investigation.

The accounting and control path is deterministic and produces the close decision.

The AI layer is advisory only. It can investigate a control exception and suggest follow-up checks, but it cannot change the close decision, override controls, approve the close, or resolve an exception.

## End-to-End Architecture

```mermaid
flowchart TD
    A["Synthetic Source Data<br/>POs / Invoices / Payments / Shared Costs / FX"] --> B["Deterministic Finance Processing"]

    B --> B1["3-Way Matching<br/>src/match.py"]
    B --> B2["Intercompany Recharge<br/>src/intercompany.py"]
    B --> B3["FX Calculations"]

    B1 --> C["Close Control Framework"]
    B2 --> C
    B3 --> C

    C --> C1["Matching Controls"]
    C --> C2["Intercompany Controls"]
    C --> C3["FX Controls"]

    C1 --> D["Close Decision"]
    C2 --> D
    C3 --> D

    D --> D1["PASS"]
    D --> D2["PASS_WITH_WARNINGS"]
    D --> D3["FAIL / BLOCKED"]

    D --> E["Immutable Close Package"]

    E --> E1["PDF Report"]
    E --> E2["Control Results"]
    E --> E3["Close Decision"]
    E --> E4["Audit Trail"]
    E --> E5["Exception Register"]
    E --> E6["SHA-256 Manifest"]

    D3 --> F["Review Workspace"]
    F --> F1["Exception Lifecycle"]
    F --> F2["Owner / Evidence / Comments"]

    F --> G["Investigation Packet"]

    G --> H1["Gemini Provider"]
    G --> H2["Claude Provider"]

    H1 --> I["13A Validation Gate"]
    H2 --> I

    I --> J["Advisory Investigation Record"]

    J --> K["Human Reviewer"]

    K -.-> F

    style D3 stroke-width:3px
    style I stroke-width:3px
    style K stroke-width:3px