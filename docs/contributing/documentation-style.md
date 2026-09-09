# Documentation style and runnable-example rules

How to write a page in [`docs/`](../README.md). These rules exist so that
twenty different authors produce something that reads like one book, and so
that a reader can tell the difference between "AQ does this" and "AQ was once
designed to do this".

Before you start, check the [documentation map](../documentation-map.md) for
which page owns your subject. If no page owns it, add it to the map first.

## Audience

Write for someone who has never used AQ and does not know the codebase. That
person is intelligent and impatient: they will read three paragraphs to decide
whether to read thirty.

Concretely:

* **Define before you use.** The first time a page says "worktree slot",
  either explain it in the sentence or link the
  [glossary](../reference/glossary.md). Never both, and never neither.
* **Purpose before mechanism.** Say what the thing is for, then how it works.
  A reader who stops after the first paragraph should have learned something
  true rather than something partial.
* **User first, contributor second.** Put what a user does at the top. Put
  internals under a heading that says so — `## How it works` or
  `## For contributors` — so the user knows where to stop.

## Page shape

Every concept and guide page carries these, in this order. Omit a section only
when it genuinely does not apply, and do not rename them.

1. **Title and one-sentence summary.** What this page is about, in a sentence
   that would make sense on its own in a search result.
2. **Why it exists.** The problem this part of AQ solves.
3. **Vocabulary.** The terms this page needs, linked to the glossary.
4. **A realistic example.** See [runnable examples](#runnable-examples).
5. **Inputs and outputs.** What goes in, what comes out, in what form.
6. **State ownership.** Which component writes this state and where it lives —
   configuration file, vault markdown, database table, or memory only. This is
   the section readers most often need and authors most often skip.
7. **Common failures and recovery.** Real symptoms, the command that diagnoses
   each, and what to do. Not "if something goes wrong, check the logs".
8. **Related pages.** Two to five links, each with a clause saying why.
9. **Source and tests.** The modules that implement this, and the focused test
   command that exercises them.

Reference pages are exempt from 2–4: be exhaustive and boring instead.

## Accuracy

**Describe `main`, and re-read the source while you write.** The repository is
actively developed; a page written from memory is a page written from an older
version.

**Label the four kinds of statement.** Blurring them is the single most common
way AQ documentation goes wrong:

| Kind | How to write it |
|---|---|
| Shipped default | "Ships enabled." / "The default is `X`." |
| Configured local policy | "This repository configures …" — and name the setting. |
| Optional compatibility | "Optional mode, off by default: …" |
| Proposed or planned | "Proposed in *spec name* (not implemented)." |

**Cite where a claim came from.** A behavioural claim names the module,
command or shipped markdown it is true of, as a relative link. That is how the
next author checks you.

**Never present retired behaviour as current.** In particular, do not describe
automatic per-task or final reviewer creation, a triage-task routing workflow,
Discord slash commands or task controls, in-process agent runtimes, or SQLite
storage as things AQ does. The
[known-inaccuracies ledger](../plans/documentation-overhaul/known-inaccuracies.md)
records where existing pages still do.

**Do not guess enum values or flags.** Read them from the source or ask the
CLI: `aq schema` prints the machine-readable command surface, and
`aq <group> <command> --help` prints the current flags.

**Numbers rot.** Counts, model names, prices and durations either come with the
command that regenerates them or do not appear. "Roughly a dozen" ages better
than a wrong exact number.

## Runnable examples

Every concept and guide page has at least one example a reader can actually
run. An example must satisfy all of these:

* **It was run.** Paste real output, trimmed with `…` where it is long. Do not
  compose plausible output by hand.
* **It is safe on a fresh install.** No destructive command without an explicit
  warning and a stated recovery. Never show a worker running `alembic upgrade`,
  `aq start`, or any migration against the operator's database — that path is
  daemon-only and documented in [migrations](../guides/migrations.md).
* **It is disposable.** Use a throwaway repository and a throwaway project, and
  end the example with the cleanup that undoes it.
* **It carries no secrets or personal paths.** Write `~/.agent-queue/…`, never
  `/home/<someone>/…`. Show `sk-…` shaped placeholders, never a real key.
* **It states its prerequisites.** "Assumes the daemon is running and project
  `demo` exists" is one line and saves the reader ten minutes.
* **It shows the expected result.** Either the output, or the follow-up command
  that proves it worked.

Format:

````markdown
```bash
aq task show demo.1
```

```text
demo.1  READY  Add a health endpoint
  profile: worker-standard-medium-claude   class: standard-medium
```
````

Use `bash` for commands and `text` for output. Keep them in separate blocks so
a reader can copy the command without the output.

## Links and formatting

* **GitHub-rendered Markdown, relative links.** Every link is relative to the
  file it is in and resolves in the GitHub file browser. There is no
  documentation site build.
* **No `[[wiki links]]`.** They render as literal brackets on GitHub. Existing
  pages use them heavily; convert them when you touch a page you own.
* **Link source files** with the path as the link text, and check the depth —
  a wrong number of `../` segments is the most common broken link in this
  repository. From a page in `docs/concepts/`:

  ```markdown
  [src/state_machine.py](../../src/state_machine.py)
  ```
* **Headings are sentence case** and unique within a page, so anchors are
  stable.
* **Tables for enumerations, prose for reasoning.** A table of nine states is
  readable; a table of nine paragraphs is not.
* **Mermaid where a picture is genuinely clearer** — a state machine, a
  sequence across components, a decision tree. GitHub renders ```` ```mermaid ````
  natively. Keep diagrams small enough to read on a phone, and never put
  information only in a diagram; the text must stand alone.
* **Admonitions** are plain blockquotes: `> **Note.**`, `> **Warning.**`.

## Checking your page before you push

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```

That verifies every tracked path still has a documentation owner and that the
coverage manifest matches the tree. If you added a module, it will tell you.

Then re-read your own page against this list:

- [ ] Someone who has never used AQ can follow the first screen of it.
- [ ] Every behavioural claim links to the source it came from.
- [ ] Every example was actually run, and its output is real.
- [ ] Shipped, configured, optional and proposed are distinguishable.
- [ ] Every link resolves from this file's directory.
- [ ] No `[[wiki link]]`, no absolute personal path, no secret.
- [ ] The page you edited is one you own in the
      [documentation map](../documentation-map.md).

Documentation-only changes do not need the full application test suite. Run the
check above and the focused tests for anything you touched.
