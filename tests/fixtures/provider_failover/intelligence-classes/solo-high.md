---
id: solo-high
name: "Fake solo · High"
description: "Provider-failover e2e kit: a class only prova can run -- the astra-high analogue."
tier: standard
thinking: high
---

```json
{
  "prova": {"model": "fake-solo-a"}
}
```

One provider only, like `astra-high`: a `solo-high-prova` task has no
equivalent rung, so it holds with `no_equivalent_rung` while `prova` is down.
