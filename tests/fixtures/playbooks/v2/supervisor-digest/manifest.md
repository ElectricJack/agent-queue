---
playbook_id: supervisor-digest
artifact_sha256: sha256:6d48ce8467b7ae621606e2be54f04f76b0b4ff11bb7a2fde43eeb1a418bee1e7
source_sha256: sha256:b205da4c310a5da0b4cbecdb5ed05475d2edde60b80c98c64b4dcf17a39f5dc5
contract_fingerprint: sha256:1107e62d25b94ea78fe362e909ff5578e3e6c0f06521863220c75dd9126733a7
questions_resolved: 0
capabilities_granted:
  aq_commands:
  - digest_request
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Playbooks V2 artifact manifest — `supervisor-digest`

The deterministic recording contains one system minute-tick command and two
terminals. It grants only `digest_request`, references no profile, and contains
no model, worker task or transport step. Structural and contract checks validate
import. The operator enables this optional policy alongside
`discord.digest.supervisor_authored`; shipping neither adds it to required or
default activations nor changes the deterministic digest.

Approved design: *Discord as a chat extension of the supervisor*
(2026-10-03, spec rev-brisk-flare r1), §4.2 and §7.1 phase P3. The supervisor
writes the three sentences in its own session through `aq digest post`; this
policy decides only when a held window becomes an author turn, and the daemon
owns cadence, quiet hours, the "nothing to report" rule, the once-a-day quiet
line, the ten-minute fallback and delivery.
