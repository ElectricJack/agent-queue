# Recorded integration-train captures

`tests/test_integration_replay.py` replays one directory per recorded stall
family named in `HISTORICAL_STALLS`. The operator or supervisor captures an
unheld live incident; workers only consume what is committed here.

```
<case>/remote.bundle   git bundle --all of the remote the train saw (sanitized)
<case>/capture.json    task rows, exact-SHA check results, holds, review targets,
                       review verdicts and the expected outcome (format 1)
```

`write_capture` in the test module writes both files from a replay `Env`; its
round-trip test is the format reference. A family with no directory reports
`GAP` and skips: its reconstructed case is never a historical pass.
