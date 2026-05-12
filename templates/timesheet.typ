// timesheet.typ — Hourly timesheet PDF template.
//
// Renders a single-page hourly timesheet from submission data. Reads the
// data from `data.yml` in the same directory (the generator writes this
// alongside a copy of the template into a working dir before invoking typst).
//
// Required data fields:
//   submission_id (str), contract_id (str), type (str), period (str),
//   submitted_date (str ISO), submitted_by (str), issue_number (int),
//   entries (list of {date, hours, description}),
//   totals ({hours, rate, amount, currency}),
//   notes (str, may be empty), status (str),
//   approved_by (str or none), approved_date (str ISO or none),
//   contractor ({name, github, email}).

#let data = yaml("data.yml")

// ── Palette ───────────────────────────────────────────────────────────────
#let primary  = rgb("#1a3a52")   // PSL-leaning dark blue
#let accent   = rgb("#2b5f8a")
#let muted    = rgb("#6b7280")
#let rule     = rgb("#d1d5db")
#let panel    = rgb("#f3f6f9")
#let pending  = rgb("#b45309")   // amber for pending state
#let approved = rgb("#15803d")   // green for approved state

// ── Page setup ────────────────────────────────────────────────────────────
#set page(
  paper: "a4",
  margin: (x: 2cm, top: 1.8cm, bottom: 1.6cm),
)
#set text(size: 10pt, fill: rgb("#1f2937"))
#set par(justify: false, leading: 0.6em)

// ── Helpers ───────────────────────────────────────────────────────────────
#let label(content) = text(size: 8pt, fill: muted, tracking: 0.5pt)[
  #upper(content)
]
#let value(content) = text(size: 10pt, weight: "regular")[#content]

// ── Header: logos + title ─────────────────────────────────────────────────
#grid(
  columns: (1fr, 1fr),
  align: (left + horizon, right + horizon),
  image("assets/quantecon-logo.png", height: 1.3cm),
  image("assets/psl-foundation-logo.png", height: 1.3cm),
)

#v(0.5cm)

#align(center)[
  #text(size: 22pt, weight: "bold", fill: primary, tracking: 1pt)[
    HOURLY TIMESHEET
  ]
  #v(-0.2cm)
  #text(size: 10pt, fill: muted)[
    Period #data.period · #data.contract_id
  ]
]

#v(0.5cm)

// ── Metadata band ─────────────────────────────────────────────────────────
#block(
  width: 100%,
  inset: (x: 0pt, y: 10pt),
  stroke: (top: 0.5pt + rule, bottom: 0.5pt + rule),
  grid(
    columns: (1fr, 1fr),
    column-gutter: 1.5em,
    row-gutter: 0.4em,
    label("Contractor"),
    label("Submission"),
    value[*#data.contractor.name*],
    value[#data.submission_id],
    value[#data.contractor.email · `@`#data.contractor.github],
    value[Submitted #data.submitted_date · `@`#data.submitted_by],
  ),
)

#v(0.7cm)

// ── Time entries table ────────────────────────────────────────────────────
#text(size: 11pt, weight: "bold", fill: primary)[Time Entries]
#v(0.25cm)

#let header_row = (
  [#label("Date")],
  [#label("Hours")],
  [#label("Description")],
)
#let entry_rows = data.entries.map(e => (
  text(size: 10pt)[#e.date],
  align(right)[#text(size: 10pt)[#e.hours_display]],
  text(size: 10pt)[#e.description],
)).flatten()

#table(
  columns: (auto, auto, 1fr),
  align: (left + horizon, right + horizon, left + horizon),
  stroke: (x, y) => (
    bottom: if y == 0 { 0.75pt + primary } else { 0.5pt + rule },
  ),
  fill: (_, y) => if y == 0 { none } else if calc.odd(y) { panel } else { none },
  inset: (x: 8pt, y: 7pt),
  ..header_row,
  ..entry_rows,
)

#v(0.5cm)

// ── Totals panel ──────────────────────────────────────────────────────────
#align(right)[
  #block(
    width: 8cm,
    inset: 14pt,
    fill: panel,
    stroke: (left: 3pt + primary),
    grid(
      columns: (1fr, auto),
      column-gutter: 1.5em,
      row-gutter: 0.45em,
      text(fill: muted)[Total hours], text(weight: "medium")[#data.totals.hours_display],
      text(fill: muted)[Hourly rate],  text(weight: "medium")[#data.totals.rate_display],
      grid.cell(colspan: 2, line(length: 100%, stroke: 0.5pt + rule)),
      text(size: 11pt, fill: primary, weight: "bold")[Amount payable],
      text(size: 13pt, fill: primary, weight: "bold")[#data.totals.amount_display],
    ),
  )
]

#v(0.5cm)

// ── Notes (if any) ────────────────────────────────────────────────────────
#if data.at("notes", default: "") != "" [
  #text(size: 11pt, weight: "bold", fill: primary)[Notes]
  #v(0.2cm)
  #block(
    width: 100%,
    inset: 10pt,
    stroke: 0.5pt + rule,
    radius: 2pt,
    text(size: 9.5pt)[#data.notes],
  )
  #v(0.5cm)
]

// ── Approval block ────────────────────────────────────────────────────────
#let is_pending = data.at("approved_by", default: none) == none

#block(
  width: 100%,
  inset: 14pt,
  fill: if is_pending { rgb("#fef3c7") } else { rgb("#dcfce7") },
  stroke: (left: 3pt + (if is_pending { pending } else { approved })),
  if is_pending [
    #text(size: 11pt, weight: "bold", fill: pending)[⚠ PENDING REVIEW]
    #v(0.2cm)
    #text(size: 9.5pt)[
      This timesheet is awaiting approval. The approved version
      (with reviewer and date) will replace this draft on merge.
    ]
  ] else [
    #text(size: 11pt, weight: "bold", fill: approved)[✓ APPROVED]
    #v(0.2cm)
    #text(size: 9.5pt)[
      Approved by `@`#data.approved_by on #data.approved_date.
    ]
  ]
)

#v(1fr)

// ── Footer ────────────────────────────────────────────────────────────────
#align(center)[
  #text(size: 8pt, fill: muted)[
    Generated by QuantEcon Timesheets · Issue \##data.issue_number ·
    Submission #data.submission_id
  ]
]
