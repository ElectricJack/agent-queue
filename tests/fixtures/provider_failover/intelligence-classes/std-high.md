---
id: std-high
name: "Fake standard · High"
description: "Provider-failover e2e kit: a class both fake providers can run, so work on it fails over."
tier: standard
thinking: high
---

```json
{
  "prova": {"model": "fake-std-a"},
  "provb": {"model": "fake-std-b"}
}
```

The failover kit's ordinary class: `std-high-prova` and `std-high-provb` are
equivalent rungs (same class, different provider).
