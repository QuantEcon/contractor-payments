# Sample receipt files

Three small, synthetic receipts — one per file type the reimbursement engine
accepts. They exist for two jobs.

## 1. Manual E2E: attachment URLs

Reimbursement claims reference receipts as GitHub attachment URLs, and there is
**no API for uploading an issue attachment** — a human has to drag the files
into an issue. These are the files to drag. Download them from this directory,
drop them into an issue in the test repo, and reuse the resulting
`https://github.com/user-attachments/...` URLs in a claim's `Receipts` section.

The amounts are deliberately coherent, so a claim built from them cross-checks
against its stated total instead of tripping the entry-sum check:

| File | Type | Amount (AUD) | Category |
|---|---|---|---|
| `receipt-taxi.png` | PNG | 48.50 | `travel` |
| `receipt-hotel.pdf` | PDF | 320.00 | `accommodation` |
| `receipt-meal.jpg` | JPEG | 26.80 | `meals` |
| | | **395.30** | |

All three categories are in the seeded `allowed_categories`, and the bundle is
~75 KB — far below both the 30 MB per-file cap and the ~25 MB email limit, so
it exercises the happy path rather than the size warnings.

## 2. Automated: real encoder output, not crafted headers

`tests/test_fetch_receipts.py` reads these files. The magic-byte tests would
otherwise assert only against hand-written byte prefixes, which cannot catch a
detector that happens to work on a synthetic 8-byte header but not on what a
real encoder emits. They also drive the extension-vs-content rule — the one
that keeps a PNG from reaching the fiscal host labelled `application/pdf` —
using genuine bytes.

Being read by the suite is also what stops them rotting: if a future change
breaks on real files, CI says so.

## Regenerating

Sources are in `src/`. Rendered with the Typst version the pipeline pins (see
`TYPST_VERSION` in `.github/workflows/process-submission.yml`):

    typst compile src/hotel.typ receipt-hotel.pdf
    typst compile --format png --ppi 150 src/taxi.typ receipt-taxi.png
    typst compile --format png --ppi 150 src/meal.typ meal.png
    sips -s format jpeg -s formatOptions 82 meal.png --out receipt-meal.jpg

Typst has no JPEG output, hence the `sips` step (macOS). Any encoder is fine —
the tests check the magic bytes, not the encoder. If you regenerate, keep the
amounts in the table above in step with the receipts themselves.
