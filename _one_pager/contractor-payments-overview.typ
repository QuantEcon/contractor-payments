// One-page system overview for QuantEcon Contractor Payments.
// Compile from the repo root so the logo paths resolve:
//   typst compile --root . _one_pager/contractor-payments-overview.typ
#set page(
  paper: "a4",
  margin: (x: 1.7cm, top: 1.3cm, bottom: 1.3cm),
  footer: align(center)[
    #text(size: 8pt, fill: rgb("#6b7280"))[
      QuantEcon Contractor Payments · contractors paid via PSL Foundation (fiscal host) ·
      #datetime.today().display("[month repr:long] [year]")
    ]
  ],
)
#set text(size: 10.5pt, fill: rgb("#1f2937"))
#set par(justify: true, leading: 0.8em, spacing: 1.05em)

#let primary = rgb("#1a3a52")
#let accent  = rgb("#2b5f8a")
#let muted    = rgb("#6b7280")
#let rule     = rgb("#d1d5db")
#let panel    = rgb("#f3f6f9")
#let green    = rgb("#15803d")

#let section(title) = block(above: 1.9em, below: 0.95em)[
  #text(fill: primary, weight: "bold", size: 12.5pt)[#title]
]

// ── Header ──────────────────────────────────────────────────────────────
#grid(
  columns: (1fr, auto),
  align: (left + horizon, right + horizon),
  column-gutter: 12pt,
  [
    #text(size: 22pt, weight: "bold", fill: primary)[Contractor Payments]\
    #v(0.1em)
    #text(size: 11pt, fill: muted)[A GitHub-native system for managing RA timesheets & invoices]
  ],
  stack(
    dir: ltr,
    spacing: 12pt,
    image("/templates/assets/quantecon-logo.png", height: 1.35cm),
    image("/templates/assets/psl-foundation-logo.png", height: 1.35cm),
  ),
)
#v(0.4em)
#line(length: 100%, stroke: 0.7pt + rule)
#v(0.7em)

// ── Core idea ───────────────────────────────────────────────────────────
#block(fill: panel, inset: 11pt, radius: 5pt, width: 100%, stroke: 0.5pt + rule)[
  #text(weight: "bold", fill: primary)[Core idea — no new tools.]
  The system runs entirely on GitHub and email, which QuantEcon, our contractors, and
  PSL Foundation already use. There is no new app to run, no dashboard to maintain, and
  nothing for PSL to log in to — just a clearer, faster way to manage timesheet and
  invoice submission.
]

// ── Problem ─────────────────────────────────────────────────────────────
#section[The problem it solves]
QuantEcon contracts a small team of research assistants and course developers, paid by
PSL Foundation as fiscal host. Timesheets and milestone invoices were handled through
ad-hoc emails and spreadsheets — slow for the admin team, opaque for contractors, and
prone to drift between what was approved and what was actually paid.

// ── How it works ────────────────────────────────────────────────────────
#section[How it works]
+ *Submit.* A contractor opens a structured issue form in their own private repository
  (hourly timesheet or milestone invoice), fills it in over the period, and submits with
  a comment.
+ *Generate.* The engine parses the entries, renders a clean PDF, and opens a pull
  request with an inline preview.
+ *Review.* The designated approver — currently *\@mmcky* — reviews the pull request
  (contract, period, amounts) and merges to approve. Approval is configurable per repo:
  any GitHub user can be set as the approver.
+ *Pay.* On approval the finished PDF is emailed automatically to PSL Foundation, the
  contract's running ledger updates, and a full audit trail is recorded.

// ── Who it serves ───────────────────────────────────────────────────────
#section[Who it serves]
- *Contractors* — a clear form, a structured record, and a reliable PDF; they submit from
  the GitHub account they already have.
- *QuantEcon admins* — review by pull request (diffs, history, audit trail); no new
  tooling to learn.
- *PSL Foundation* — a consistently formatted PDF delivered by email on approval, ready
  to process for payment.

// ── Supported / planned ─────────────────────────────────────────────────
#section[What's supported, what's planned]
#table(
  columns: (1fr, 1fr),
  stroke: 0.5pt + rule,
  inset: 9pt,
  table.header(
    text(fill: green, weight: "bold")[✓ Supported now],
    text(fill: accent, weight: "bold")[→ Planned],
  ),
  [
    - Hourly timesheets & milestone invoices
    - Multi-currency (AUD / USD / JPY)
    - Per-contractor private repos + one-step onboarding
    - Review & approval by pull request
    - Approved PDF emailed to PSL Foundation
    - Running ledger per contract
    - Revisions & supplemental invoices
  ],
  [
    - Reimbursement / expense claims \ (receipts, multi-currency)
    - Automated branch protection \ (dedicated GitHub App)
    - Cross-contractor reporting & dashboards
  ],
)

#v(0.9em)
#block(fill: primary, inset: 10pt, radius: 5pt, width: 100%)[
  #text(fill: white, weight: "bold")[Status — June 2026:]
  #text(fill: white)[live in production. First contractors onboarded on hourly JPY
  contracts; approved timesheets now flow automatically through review to PSL Foundation
  for payment.]
]
