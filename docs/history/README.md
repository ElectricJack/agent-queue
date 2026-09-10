# Historical material and dispositions

AQ keeps design notes, migration records, reports, and previous reviews in the
repository so a contributor can understand a decision’s context. They are not
the operating manual. Start at the [documentation home](../README.md) for
current behaviour, then use this page when you need historical evidence.

## How to read these directories

| Material | Disposition | Use it for |
| --- | --- | --- |
| [`docs/specs/`](../specs/) and [`docs/superpowers/`](../superpowers/) | Historical or proposed unless a current page says otherwise. | Design rationale and intentionally unshipped proposals. |
| [`docs/reports/`](../reports/), [`docs/reviews/`](../reviews/), and [`docs/analysis/`](../analysis/) | Point-in-time evidence. | Incident context, audits, and dated decisions. |
| [`docs/plans/`](../plans/) | Planning record. | Scope and ownership of work, not runtime instructions. |
| [`notes/`](../../notes/) and [`reports/`](../../reports/) | Historical working material. | Repository archaeology. |
| Current concepts, guides, tutorials, and reference pages | Current. | Operating AQ today. |

The [known-inaccuracies ledger](../plans/documentation-overhaul/known-inaccuracies.md)
calls out the remaining pages that can describe retired defaults. In particular,
do not infer current support for SQLite, in-process coding runtimes, Discord
task controls, or automatic reviewer-task flows from an old document.

## Recovery when documents disagree

1. Prefer the current concept, guide, or reference page linked from the
   [documentation home](../README.md).
2. Follow that page’s source links and run the stated focused command or test.
3. Treat a dated spec or report as evidence about its date. If it conflicts
   with current source, record the discrepancy in the known-inaccuracies ledger
   instead of changing runtime behaviour to match history.

## Related pages

* [Documentation map](../documentation-map.md) — ownership and page genres.
* [Final coverage and disposition report](../plans/documentation-overhaul/final-coverage-report.md) — the final assembly’s verification record.
