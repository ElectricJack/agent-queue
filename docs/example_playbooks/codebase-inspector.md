---
id: codebase-inspector
triggers:
  - timer.4h
scope: system
---

# Codebase Inspector

Inspect a random section of the codebase for quality issues,
security risks, and documentation gaps. Follow weighted selection:
source (40%), specs (20%), tests (15%), config (10%), recent
changes (15%). Check inspection history to avoid re-inspecting
the same files. Only report concrete, actionable findings.

## Tools

1. Call `select_files_for_inspection` (files plugin) with the
   target `project_id` to get a weighted, history-aware sample of
   files to inspect. The tool handles categorization, exclusion
   of binary/generated files, and history-based de-duplication
   automatically.
2. For each selected file, call `read_file` to obtain its
   contents (respect the size guidance — the tool's output
   includes `lines_returned`/`truncated` flags).
3. After inspecting a file, call `record_file_inspection` with
   the `file_path`, an optional `summary`, and `findings_count`
   so future runs of this playbook can skip recently-covered
   files.

If `select_files_for_inspection` returns an empty `files` list,
log the inspection as complete-but-skipped and exit — do NOT
create a task to extend the tool; the tool already handles the
selection logic.

If the system health check recently flagged a related issue,
consolidate into one task rather than creating duplicates.
