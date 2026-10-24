---
playbook_id: quilt-health-watch
artifact_sha256: sha256:68115d334888c6644b7799a93360a11918cd1bac4ab153b065ea995318eb77f7
source_sha256: sha256:eee0fc8aaa2ffc75b17748caef24c16c2c81552afaeb1932352c3588414156cc
contract_fingerprint: sha256:55b748774da8a820d323636666900b4072b113b50d1e4b3ae65c32908e604603
questions_resolved: 0
profiles_referenced: []
---

# Migration candidate

Reviewed 2026-09-29 by the supervisor at Jack's request ("Why are playbooks not loading, fix it"). The only semantic change from v1 (sha256:1106457d…) is the routing ratchet migration: the three check steps (pre-market, market-hours, off-hours) drop the retired worker pin `fast-low-claude` and carry the class hint `fast-low`; the router picks the worker. Triggers, schedules, health-check procedure and escalation are unchanged. Approved for import and activation.
