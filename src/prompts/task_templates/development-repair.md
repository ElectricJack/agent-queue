The development batch parked these source revisions:
{{sources}}
Candidate/base: {{candidate_sha}}.
Source branches:
{{branches}}
Publication target: refs/heads/{{target_branch}}.
{{recovery}}Publish the repair on your own task branch. Ordinary merge commits are allowed. Run focused local checks and close with actual evidence; no parent verifier or PR is required. A passing close attests that every listed source revision is resolved; once your repair is delivered to {{target_branch}}, that delivery also satisfies those sources for their successors. Do not push {{target_branch}}. The development publisher will collect your branch. This is one resumable repair task; queue and provider waits do not expire it.{{failure_details}}