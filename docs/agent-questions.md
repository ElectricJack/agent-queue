# Questions from live agents

AQ watches native Codex and Claude transcripts for completed assistant turns
that ask for input while a worker holds an active task. It records the question
against the exact task claim and session instance. Named supervisor sessions and
unassigned interactive terminals do not recursively create questions.

Routine factual questions are sent to the global supervisor. Approval requests,
scope/design decisions, risky actions, and ambiguous questions go to the human.
The supervisor can escalate a routine question and cannot supply a human-only
approval. Unanswered supervisor questions escalate after five minutes.

## Answer a question

- In Discord, use **Reply** on the agent-question card. The button remains usable
  after an AQ restart. Only configured authorized Discord users can submit it.
- In the dashboard, **Waiting for input** appears in the Agent flock. Open the
  agent's terminal and answer directly to continue its existing conversation.
- From the local operator CLI:

  ```sh
  aq question list
  aq question answer QUESTION_ID --body "Your answer"
  aq question escalate QUESTION_ID --reason "This needs a human decision"
  ```

Supervisor sessions use their existing scoped CLI credentials. Worker tokens
cannot impersonate human callers. General MCP/LLM command calls without an
operator identity cannot approve questions.

An answer is queued durably and submitted into the same live terminal. AQ never
clears a terminal draft or reassigns/restarts the task to deliver an answer. If
there is a draft, the answer waits until the input is available. Late answers
cannot follow a reused tmux name into a replacement worker or a different claim.

## Waiting and recovery

Waiting retains the worker and its workspace. Stall recovery is suspended for
the matching pending question; saved stall counters are not reset. A direct user
reply resolves the question. AQ's own stall reminder does not count as a reply.
After a question resumes a task, the stuck-session backstop uses activity so time
spent waiting for the user does not immediately kill the resumed worker.

Questions and accepted answers survive daemon restarts. Discord records actual
successful delivery separately from queue delivery and retries enrolled failures;
it does not replay old message history when the feature is first enabled. If no
Discord destination is available, the pending question remains visible in AQ.
As with any external transport, a process crash after Discord accepts a message
but before the receipt commits can leave a duplicate notification on retry.
Terminal input has the same small crash window: if the process dies after input
is submitted but before its receipt commits, delivery may repeat after recovery.
Normal retries and competing replies are deduplicated.

This feature handles assistant questions in native conversation transcripts. It
does not automatically approve harness permission dialogs or grant additional
filesystem, network, API, or project access. Ended sessions are not rebound to a
new worker to deliver historical answers.
