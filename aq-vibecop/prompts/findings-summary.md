# Vibecop Findings Summary

You ran vibecop static analysis and found **$total_findings** issue(s) across **$files_scanned** file(s).

## Severity Breakdown
- Errors: $error_count
- Warnings: $warning_count
- Info: $info_count

## Findings

$findings_detail

## Recommended Actions

1. **Fix all errors first** — these indicate bugs, security vulnerabilities, or correctness issues that will cause problems.
2. **Address warnings** — these are code quality antipatterns (god-functions, excessive-any, dead-code) that make the code harder to maintain.
3. **Review info items** — these are suggestions for improvement but are not blocking.

When fixing findings:
- Run `vibecop_check` on the specific files you modified to verify fixes.
- If a finding is a false positive, you can suppress it with a `.vibecop.yml` config or inline comment.
- Focus on the detector ID (e.g., `god-function`, `sql-injection`) to understand the pattern being flagged.
