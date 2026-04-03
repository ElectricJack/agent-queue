# Vibecop Scan Results

**Scan path:** ${scan_path}
**Detectors used:** ${detectors_used}

## Severity Breakdown

| Severity | Count |
|----------|-------|
| Error    | ${error_count} |
| Warning  | ${warning_count} |
| Info     | ${info_count} |

## Top Findings

${top_findings}

## All Findings

${findings}

---

## How to Read Findings

Each finding includes:
- **File path** and **line number** — where the antipattern was detected
- **Detector ID** — the specific rule that triggered (e.g. `god-function`, `sql-injection`)
- **Description** — what the detector found and why it matters
- **Suggested fix** — a concrete action to resolve the finding

## Why These Antipatterns Matter

- **Error-severity findings** indicate bugs, security vulnerabilities, or correctness issues that will cause problems in production. These must be fixed before completing your task.
- **Warning-severity findings** are code quality antipatterns (god-functions, excessive-any, dead-code-paths) that make code harder to maintain and more likely to introduce future bugs.
- **Info-severity findings** are suggestions for improvement — not blocking but worth considering.

## Recommended Actions

1. **Fix all errors first** — these are blocking issues that indicate bugs, security vulnerabilities, or correctness problems.
2. **Address warnings** — these are code quality antipatterns (god-functions, excessive-any, dead-code) that make code harder to maintain.
3. **Review info items** — these are suggestions for improvement but are not blocking.

When fixing findings:
- Run `vibecop_check` on the specific files you modified to verify your fixes resolved the findings.
- If a finding is a false positive, you can suppress it with a `.vibecop.yml` config or inline comment.
- Focus on the detector ID (e.g. `god-function`, `sql-injection`) to understand the pattern being flagged.
- After fixing, re-run `vibecop_scan` with the same `diff_ref` to confirm a clean scan.
