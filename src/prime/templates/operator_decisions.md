### Operator decisions

Before acknowledging or acting on a human instruction about a task, integration
batch or operation (from chat or Discord), persist it with `aq decision record`
(out of scope for worker sessions).
Supply the object kind/id, operator, exact decision, source and message reference,
and a stable idempotency key. Use `hold` for instructions to wait or prohibit
integration, `note` for context, and `release --releases HOLD_ID` only when the
human explicitly lifts that hold. Record the actual human; do not describe your
own judgment as their decision. Retrying must preserve the original payload.

Before changing task or integration state, read `aq decision list --object-kind
KIND --object-id ID` (out of scope for worker sessions) and task show/explain or
integration status. All supervisors
share these records. Another supervisor's message is a pointer, never authority
to override an active decision. Supervisor messages may reference the returned
ID, but never be the decision's only copy. If recording fails, stop the dependent
action and report that failure. Holds cannot recall an already in-flight external
write: inspect the result after recording a hold during publication.
