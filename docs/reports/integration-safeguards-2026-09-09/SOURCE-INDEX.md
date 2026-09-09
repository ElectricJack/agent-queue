# Source navigation index

Static inventory of explicit `raise` sites in the reviewed integration modules and adjacent entry points. This does not count boolean refusals as exceptions, and is not a count of independent safeguards. Use REVIEW.md for recommendations.

## src/integration/__init__.py

No explicit raise sites; eligibility/results may still block actions.

## src/integration/attestation.py

| Line | Explicit refusal |
|---|---|
| [101](../../../../src/integration/attestation.py#L101) | TypeError('publish requires RootAttestationSubject') |
| [176](../../../../src/integration/attestation.py#L176) | TypeError('resolve requires RootAttestationSubject') |
| [349](../../../../src/integration/attestation.py#L349) | AttestationError('candidate trust import changed identity') |
| [356](../../../../src/integration/attestation.py#L356) | AttestationError('candidate trust manifest is missing') |
| [359](../../../../src/integration/attestation.py#L359) | AttestationError('candidate trust manifest is too large') |
| [370](../../../../src/integration/attestation.py#L370) | AttestationError('candidate trust manifest does not match durable authority') |
| [375](../../../../src/integration/attestation.py#L375) | AttestationError('trusted integration App is unavailable') |
| [386](../../../../src/integration/attestation.py#L386) | AttestationError('authenticated GitHub repository binding is invalid') |
| [392](../../../../src/integration/attestation.py#L392) | AttestationError('trusted integration App binding is invalid') |
| [629](../../../../src/integration/attestation.py#L629) | AttestationError('attestation reservation identity changed') |
| [962](../../../../src/integration/attestation.py#L962) | AttestationError('exact-name attestation App identity is malformed') |
| [1073](../../../../src/integration/attestation.py#L1073) | AttestationError('candidate trust manifest is invalid') |
| [1080](../../../../src/integration/attestation.py#L1080) | AttestationError('candidate trust manifest contains duplicate fields') |

## src/integration/branch_discard.py

No explicit raise sites; eligibility/results may still block actions.

## src/integration/branch_materialization.py

No explicit raise sites; eligibility/results may still block actions.

## src/integration/candidate_ci.py

No explicit raise sites; eligibility/results may still block actions.

## src/integration/candidates.py

| Line | Explicit refusal |
|---|---|
| [253](../../../../src/integration/candidates.py#L253) | ValueError('candidate repair budget could not be activated') |
| [298](../../../../src/integration/candidates.py#L298) | RuntimeError(ancestry.stderr or 'candidate ancestry check failed') |
| [307](../../../../src/integration/candidates.py#L307) | ValueError('candidate rebuild base must be an exact Git OID') |
| [370](../../../../src/integration/candidates.py#L370) | ValueError('current candidate revision is not recoverable') |
| [536](../../../../src/integration/candidates.py#L536) | ValueError('CI repair preservation requires a completed candidate') |
| [542](../../../../src/integration/candidates.py#L542) | ValueError('persisted CI repair is not an exact commit') |
| [546](../../../../src/integration/candidates.py#L546) | ValueError('current candidate lost its accepted CI repair ancestry') |
| [552](../../../../src/integration/candidates.py#L552) | RuntimeError(tree.stderr or 'CI repair preservation merge failed') |
| [565](../../../../src/integration/candidates.py#L565) | RuntimeError(commit.stderr or 'CI repair preservation commit failed') |
| [642](../../../../src/integration/candidates.py#L642) | CandidateAuthorizationError('candidate repair requires a current session writer') |
| [649](../../../../src/integration/candidates.py#L649) | ValueError('candidate repair contains a non-OID') |
| [682](../../../../src/integration/candidates.py#L682) | CandidateAuthorizationError('candidate repair batch is absent') |
| [759](../../../../src/integration/candidates.py#L759) | CandidateAuthorizationError('candidate repair writer authority is stale') |
| [832](../../../../src/integration/candidates.py#L832) | CandidateAuthorizationError('candidate repair reservation identity differs') |
| [839](../../../../src/integration/candidates.py#L839) | CandidateAuthorizationError('caller repair lineage is not authority; a pushed server reservation is required') |
| [844](../../../../src/integration/candidates.py#L844) | CandidateAuthorizationError('candidate repair reservation does not exist') |
| [848](../../../../src/integration/candidates.py#L848) | CandidateAuthorizationError('candidate repair reservation has not been pushed') |
| [946](../../../../src/integration/candidates.py#L946) | CandidateAuthorizationError('candidate repair reservation does not exist') |
| [953](../../../../src/integration/candidates.py#L953) | CandidateAuthorizationError('candidate repair reservation has not been pushed') |
| [1181](../../../../src/integration/candidates.py#L1181) | CandidateStaleAuthority('candidate repair acceptance CAS lost') |
| [1200](../../../../src/integration/candidates.py#L1200) | CandidateStaleAuthority('candidate repair handoff reservation is stale') |
| [1243](../../../../src/integration/candidates.py#L1243) | CandidateStaleAuthority('candidate repair handoff authority changed') |
| [1255](../../../../src/integration/candidates.py#L1255) | CandidateStaleAuthority('candidate repair handoff owner changed') |
| [1263](../../../../src/integration/candidates.py#L1263) | CandidateStaleAuthority('candidate repair handoff confirmation is absent') |
| [1279](../../../../src/integration/candidates.py#L1279) | CandidateStaleAuthority('candidate repair handoff fence prediction changed') |
| [1297](../../../../src/integration/candidates.py#L1297) | CandidateStaleAuthority('candidate repair handoff persistence CAS lost') |
| [1337](../../../../src/integration/candidates.py#L1337) | CandidateAuthorizationError('candidate repair push authority is absent') |
| [1355](../../../../src/integration/candidates.py#L1355) | CandidateStaleAuthority('candidate repair publication is waiting') |
| [1384](../../../../src/integration/candidates.py#L1384) | CandidateStaleAuthority('candidate repair push CAS lost') |
| [1397](../../../../src/integration/candidates.py#L1397) | CandidateAuthorizationError('candidate repair batch is absent') |
| [1460](../../../../src/integration/candidates.py#L1460) | CandidateAuthorizationError('candidate repair push authority is stale') |
| [1476](../../../../src/integration/candidates.py#L1476) | CandidateAuthorizationError('candidate repair workspace path is absent') |
| [1479](../../../../src/integration/candidates.py#L1479) | CandidateAuthorizationError('candidate repair workspace path is not absolute') |
| [1483](../../../../src/integration/candidates.py#L1483) | CandidateAuthorizationError('candidate repair workspace path does not resolve') |
| [1487](../../../../src/integration/candidates.py#L1487) | CandidateAuthorizationError('candidate repair workspace path is not a directory') |
| [1502](../../../../src/integration/candidates.py#L1502) | ValueError('integration batch does not exist') |
| [1515](../../../../src/integration/candidates.py#L1515) | ValueError('integration batch project does not exist') |
| [1528](../../../../src/integration/candidates.py#L1528) | ValueError('integration batch does not exist') |
| [1530](../../../../src/integration/candidates.py#L1530) | ValueError('integration batch project identity changed') |
| [1535](../../../../src/integration/candidates.py#L1535) | ValueError('integration batch is outside active train authority') |
| [1550](../../../../src/integration/candidates.py#L1550) | ValueError('integration batch member ordinals are incomplete') |
| [1587](../../../../src/integration/candidates.py#L1587) | ValueError('integration batch operation is missing') |
| [1703](../../../../src/integration/candidates.py#L1703) | CandidateStaleAuthority('batch changed while reserving revision') |
| [1821](../../../../src/integration/candidates.py#L1821) | RuntimeError(committed.stderr or 'candidate commit failed') |
| [1843](../../../../src/integration/candidates.py#L1843) | CandidateStaleAuthority('candidate completion CAS lost') |
| [1981](../../../../src/integration/candidates.py#L1981) | CandidateStaleAuthority('candidate ref publication CAS lost') |
| [2022](../../../../src/integration/candidates.py#L2022) | ValueError('audit PR identity does not match candidate') |
| [2061](../../../../src/integration/candidates.py#L2061) | CandidateStaleAuthority('publication canonical PR differs') |
| [2113](../../../../src/integration/candidates.py#L2113) | CandidateStaleAuthority('candidate pending insert raced') |
| [2149](../../../../src/integration/candidates.py#L2149) | CandidateStaleAuthority('candidate progress CAS lost') |
| [2223](../../../../src/integration/candidates.py#L2223) | CandidateStaleAuthority('candidate authority changed') |
| [2246](../../../../src/integration/candidates.py#L2246) | CandidateStaleAuthority('candidate branch fence changed') |
| [2319](../../../../src/integration/candidates.py#L2319) | CandidateStaleAuthority('candidate mutation reservation raced without canonical state') |
| [2355](../../../../src/integration/candidates.py#L2355) | CandidateStaleAuthority('candidate mutation identity changed') |
| [2547](../../../../src/integration/candidates.py#L2547) | CandidateStaleAuthority('candidate mutation reconciliation CAS lost') |
| [2898](../../../../src/integration/candidates.py#L2898) | CandidateStaleAuthority('candidate repair revision ancestry is invalid') |
| [2912](../../../../src/integration/candidates.py#L2912) | CandidateStaleAuthority('candidate repair revision is missing') |
| [2925](../../../../src/integration/candidates.py#L2925) | CandidateStaleAuthority('accepted candidate repair identity changed') |
| [2944](../../../../src/integration/candidates.py#L2944) | CandidateStaleAuthority('accepted candidate repair lineage is invalid') |
| [2972](../../../../src/integration/candidates.py#L2972) | CandidateStaleAuthority('candidate conflict member changed') |
| [2985](../../../../src/integration/candidates.py#L2985) | CandidateStaleAuthority('candidate conflict member CAS lost') |
| [3000](../../../../src/integration/candidates.py#L3000) | CandidateStaleAuthority('candidate conflict batch CAS lost') |
| [3017](../../../../src/integration/candidates.py#L3017) | CandidateStaleAuthority('partial candidate publication is waiting') |
| [3236](../../../../src/integration/candidates.py#L3236) | ValueError('candidate repository is unavailable') |
| [3243](../../../../src/integration/candidates.py#L3243) | CandidateAuthorizationError('candidate App repository does not match the frozen repository') |
| [3256](../../../../src/integration/candidates.py#L3256) | RuntimeError(result.stderr or 'candidate retained store initialization failed') |
| [3299](../../../../src/integration/candidates.py#L3299) | RuntimeError(result.stderr or 'candidate recovery pin failed') |
| [3333](../../../../src/integration/candidates.py#L3333) | RuntimeError(result.stderr or 'candidate author scan failed') |

## src/integration/ci.py

| Line | Explicit refusal |
|---|---|
| [46](../../../../src/integration/ci.py#L46) | ValueError('required check names must be unique and non-empty') |
| [64](../../../../src/integration/ci.py#L64) | ValueError('numeric CI producer identity must be positive') |
| [80](../../../../src/integration/ci.py#L80) | AttestationError('CI policy is malformed') |
| [88](../../../../src/integration/ci.py#L88) | AttestationError(f'{boundary} CI policy is missing required checks') |
| [91](../../../../src/integration/ci.py#L91) | AttestationError('CI policy producer identity is malformed') |
| [104](../../../../src/integration/ci.py#L104) | AttestationError('CI policy producer or required checks are malformed') |
| [122](../../../../src/integration/ci.py#L122) | ValueError('CI and attestation App identities must be distinct') |
| [172](../../../../src/integration/ci.py#L172) | ValueError('attested checks contain duplicates') |
| [174](../../../../src/integration/ci.py#L174) | ValueError('workflow attempts contain duplicate suites') |
| [177](../../../../src/integration/ci.py#L177) | ValueError('workflow attempt coverage does not match check suites') |
| [181](../../../../src/integration/ci.py#L181) | ValueError('attestation head identity is incoherent') |
| [189](../../../../src/integration/ci.py#L189) | AttestationError('attestation JSON is invalid') |
| [192](../../../../src/integration/ci.py#L192) | AttestationError('attestation bytes are noncanonical') |
| [260](../../../../src/integration/ci.py#L260) | AttestationError('exact-name attestation App identity is malformed') |
| [265](../../../../src/integration/ci.py#L265) | AttestationError('trusted attestation ordering identity is malformed') |
| [268](../../../../src/integration/ci.py#L268) | AttestationError('trusted attestation is missing') |
| [276](../../../../src/integration/ci.py#L276) | AttestationError('newest trusted attestation is not successful') |
| [279](../../../../src/integration/ci.py#L279) | AttestationError('newest trusted attestation payload is missing') |
| [282](../../../../src/integration/ci.py#L282) | AttestationError('newest trusted attestation digest does not match') |
| [293](../../../../src/integration/ci.py#L293) | AttestationError('newest trusted attestation identity does not match') |
| [298](../../../../src/integration/ci.py#L298) | AttestationError('newest trusted attestation is invalid') |
| [370](../../../../src/integration/ci.py#L370) | ValueError('CI evidence must bind exactly one typed subject') |
| [372](../../../../src/integration/ci.py#L372) | ValueError('CI evidence checks must be non-empty') |
| [413](../../../../src/integration/ci.py#L413) | AttestationError('CI attempt was already bound to another subject') |
| [430](../../../../src/integration/ci.py#L430) | TypeError('CIService requires an authenticated or explicit fixture observer') |
| [442](../../../../src/integration/ci.py#L442) | ValueError('authenticated provider identity does not match CI trust') |
| [451](../../../../src/integration/ci.py#L451) | TypeError('observe_parent requires ParentCISubject') |
| [479](../../../../src/integration/ci.py#L479) | TypeError('observe_candidate requires CandidateCISubject') |
| [567](../../../../src/integration/ci.py#L567) | AttestationError('candidate changed before aggregate green projection') |
| [788](../../../../src/integration/ci.py#L788) | AttestationError('trusted observation omitted workflow identity') |
| [873](../../../../src/integration/ci.py#L873) | AttestationError('invalid CI head') |
| [889](../../../../src/integration/ci.py#L889) | AttestationError(f'required check App identity is malformed: {name}') |
| [896](../../../../src/integration/ci.py#L896) | AttestationError(f'required check ordering identity is malformed: {name}') |
| [901](../../../../src/integration/ci.py#L901) | AttestationError(f'required check is missing: {name}') |
| [910](../../../../src/integration/ci.py#L910) | AttestationError(f'required check App identity is malformed: {name}') |
| [921](../../../../src/integration/ci.py#L921) | AttestationError(f'required check is not conclusive: {name}') |
| [951](../../../../src/integration/ci.py#L951) | AttestationError('workflow attempt identity is missing or ambiguous') |
| [962](../../../../src/integration/ci.py#L962) | AttestationError('workflow attempt ordering identity is malformed') |
| [979](../../../../src/integration/ci.py#L979) | AttestationError('workflow attempt is not conclusive') |
| [1067](../../../../src/integration/ci.py#L1067) | AttestationError('trusted attestation App identity is malformed') |
| [1072](../../../../src/integration/ci.py#L1072) | AttestationError('trusted attestation ordering identity is malformed') |
| [1107](../../../../src/integration/ci.py#L1107) | AttestationError('published attestation identity is malformed') |
| [1132](../../../../src/integration/ci.py#L1132) | AttestationError('CI receipt identity does not match policy trust') |
| [1135](../../../../src/integration/ci.py#L1135) | AttestationError('attestation identity does not match trust manifest') |
| [1146](../../../../src/integration/ci.py#L1146) | AttestationError('attestation identity does not match trust manifest') |
| [1194](../../../../src/integration/ci.py#L1194) | AttestationError('workflow repository identity does not match CI trust') |
| [1211](../../../../src/integration/ci.py#L1211) | AttestationError('latest workflow attempt job identity is malformed') |
| [1213](../../../../src/integration/ci.py#L1213) | AttestationError('latest workflow attempt job identity is ambiguous') |
| [1229](../../../../src/integration/ci.py#L1229) | AttestationError(f'required check is not from the latest workflow attempt: {check['name']}') |
| [1243](../../../../src/integration/ci.py#L1243) | ValueError('CI receipt checks contain duplicates') |
| [1245](../../../../src/integration/ci.py#L1245) | ValueError('CI receipt workflow attempts contain duplicate suites') |
| [1248](../../../../src/integration/ci.py#L1248) | ValueError('CI receipt workflow attempt coverage does not match check suites') |
| [1254](../../../../src/integration/ci.py#L1254) | ValueError('CI receipt head identity is incoherent') |
| [1261](../../../../src/integration/ci.py#L1261) | AttestationError('duplicate attestation field') |

## src/integration/cleanup.py

| Line | Explicit refusal |
|---|---|
| [636](../../../../src/integration/cleanup.py#L636) | ValueError('cleanup ref must be a complete head ref') |
| [905](../../../../src/integration/cleanup.py#L905) | ValueError('retained worktree provenance is incomplete') |
| [933](../../../../src/integration/cleanup.py#L933) | ValueError('cleanup PR identity does not match repository') |
| [936](../../../../src/integration/cleanup.py#L936) | ValueError('cleanup PR number is invalid') |

## src/integration/collection.py

No explicit raise sites; eligibility/results may still block actions.

## src/integration/completion_recovery.py

No explicit raise sites; eligibility/results may still block actions.

## src/integration/controls.py

| Line | Explicit refusal |
|---|---|
| [412](../../../../src/integration/controls.py#L412) | ValueError('integration mode must be disabled, observe, hierarchy, or train') |
| [414](../../../../src/integration/controls.py#L414) | ValueError('expected generation, reason, and operator are required') |
| [417](../../../../src/integration/controls.py#L417) | ValueError('integration schedule interval must be a positive integer') |
| [419](../../../../src/integration/controls.py#L419) | ValueError('integration schedule interval must be positive') |
| [421](../../../../src/integration/controls.py#L421) | ValueError('integration schedule interval is only valid with train mode') |
| [528](../../../../src/integration/controls.py#L528) | ValueError('history waiver is stale or already consumed') |
| [592](../../../../src/integration/controls.py#L592) | ValueError('history waiver is stale or already consumed') |
| [650](../../../../src/integration/controls.py#L650) | ValueError('only repository, review mode, and hierarchical policy are configurable here') |
| [654](../../../../src/integration/controls.py#L654) | ValueError('expected integration generation must be non-negative') |
| [661](../../../../src/integration/controls.py#L661) | ValueError('integration_repository and integration_repository_id are mutually exclusive') |
| [669](../../../../src/integration/controls.py#L669) | ValueError('integration_repository must contain exactly id, url, and default_branch') |
| [678](../../../../src/integration/controls.py#L678) | ValueError('integration_repository fields must be non-empty strings') |
| [680](../../../../src/integration/controls.py#L680) | ValueError('integration review mode must be pull_request') |
| [689](../../../../src/integration/controls.py#L689) | ValueError('integration routes must be system-scoped or scoped to the configured project') |
| [824](../../../../src/integration/controls.py#L824) | RuntimeError('integration repository lost its project ownership fence') |
| [837](../../../../src/integration/controls.py#L837) | RuntimeError('integration configuration lost its generation fence') |
| [883](../../../../src/integration/controls.py#L883) | ValueError('expected generation, reason, and operator are required') |

## src/integration/hierarchy.py

| Line | Explicit refusal |
|---|---|
| [61](../../../../src/integration/hierarchy.py#L61) | HierarchyError('invalid', 'materialization base is not an exact Git OID') |
| [64](../../../../src/integration/hierarchy.py#L64) | GitError(remote.error or 'remote branch state is unknown') |
| [67](../../../../src/integration/hierarchy.py#L67) | HierarchyError('delivery_target_fixed', 'branch exists at an unexpected commit') |
| [81](../../../../src/integration/hierarchy.py#L81) | GitError(fetched.stderr or 'could not fetch pinned materialization base') |
| [93](../../../../src/integration/hierarchy.py#L93) | HierarchyError('invalid', 'remote did not confirm the pinned branch ref') |
| [106](../../../../src/integration/hierarchy.py#L106) | HierarchyError('dirty', 'task has no exact owned integration workspace') |
| [110](../../../../src/integration/hierarchy.py#L110) | HierarchyError('dirty', 'workspace is not on the canonical task branch') |
| [113](../../../../src/integration/hierarchy.py#L113) | HierarchyError('dirty', 'workspace has uncommitted changes') |
| [116](../../../../src/integration/hierarchy.py#L116) | HierarchyError('dirty', 'workspace HEAD is not an exact Git OID') |
| [121](../../../../src/integration/hierarchy.py#L121) | HierarchyError('dirty', 'workspace HEAD is not exactly pushed') |
| [135](../../../../src/integration/hierarchy.py#L135) | HierarchyError('dirty', 'repair lineage is not an exact Git OID pair') |
| [140](../../../../src/integration/hierarchy.py#L140) | HierarchyError('dirty', 'repair HEAD does not descend from its bound subject') |
| [151](../../../../src/integration/hierarchy.py#L151) | HierarchyError('dirty', 'repair commit lineage is incomplete or invalid') |
| [157](../../../../src/integration/hierarchy.py#L157) | HierarchyError('dirty', 'repair HEAD parent lineage is invalid') |
| [173](../../../../src/integration/hierarchy.py#L173) | HierarchyError('dirty', 'task has no exact owned integration workspace') |
| [183](../../../../src/integration/hierarchy.py#L183) | HierarchyError('dirty', 'caller head does not match workspace HEAD') |
| [222](../../../../src/integration/hierarchy.py#L222) | HierarchyError('invalid', 'at least one child is required') |
| [231](../../../../src/integration/hierarchy.py#L231) | HierarchyError('stale_parent', f'expected generation {expected_generation}, found {checkpoint['generation']}') |
| [237](../../../../src/integration/hierarchy.py#L237) | HierarchyError('invalid', 'parent checkpoint does not name an exact commit') |
| [257](../../../../src/integration/hierarchy.py#L257) | HierarchyError('invalid', 'hierarchical child exceeds the naming cap') |
| [326](../../../../src/integration/hierarchy.py#L326) | HierarchyError('dirty', 'leaf completion head is not verifiable') |
| [334](../../../../src/integration/hierarchy.py#L334) | HierarchyError('dirty', 'leaf checkpoint verifier returned another head') |
| [343](../../../../src/integration/hierarchy.py#L343) | HierarchyError('invariant_error', 'leaf completion has children') |
| [346](../../../../src/integration/hierarchy.py#L346) | HierarchyError('invariant_error', 'parent episode cannot close as a leaf') |
| [385](../../../../src/integration/hierarchy.py#L385) | HierarchyError('dirty', 'filing parent HEAD is not an exact Git OID') |
| [388](../../../../src/integration/hierarchy.py#L388) | HierarchyError('stale_parent', f'expected generation {expected_generation}, found {current_generation}') |
| [396](../../../../src/integration/hierarchy.py#L396) | HierarchyError('invalid', 'hierarchical child exceeds the naming cap') |
| [459](../../../../src/integration/hierarchy.py#L459) | HierarchyError('invalid', 'hierarchical child exceeds the naming cap') |
| [627](../../../../src/integration/hierarchy.py#L627) | HierarchyError('dirty', 'head_sha must be a lowercase 40-character Git OID') |
| [629](../../../../src/integration/hierarchy.py#L629) | HierarchyError('dirty', 'workspace checkpoint verifier is unavailable') |
| [691](../../../../src/integration/hierarchy.py#L691) | HierarchyError('dirty', 'checkpoint verifier returned another head') |
| [695](../../../../src/integration/hierarchy.py#L695) | HierarchyError('stale_head', 'receipt carry-forward ancestry verifier is unavailable') |
| [702](../../../../src/integration/hierarchy.py#L702) | HierarchyError('stale_head', 'new parent checkpoint does not contain verified aggregate') |
| [716](../../../../src/integration/hierarchy.py#L716) | HierarchyError('delivery_target_fixed', 'parent checkpoint branch identity changed') |
| [720](../../../../src/integration/hierarchy.py#L720) | HierarchyError('stale', f'expected generation {generation}, found {checkpoint['generation']}') |
| [791](../../../../src/integration/hierarchy.py#L791) | HierarchyError('invalid', 'branch materializer is unavailable') |
| [799](../../../../src/integration/hierarchy.py#L799) | HierarchyError('invalid', 'pending origin does not exist') |
| [803](../../../../src/integration/hierarchy.py#L803) | HierarchyError('invalid', 'origin repository does not exist') |
| [809](../../../../src/integration/hierarchy.py#L809) | HierarchyError('invalid', 'origin branch has no task ownership') |
| [827](../../../../src/integration/hierarchy.py#L827) | HierarchyError('invalid', 'pending origin changed') |
| [834](../../../../src/integration/hierarchy.py#L834) | HierarchyError('invalid', 'materialized ref is not the pinned base') |
| [844](../../../../src/integration/hierarchy.py#L844) | HierarchyError('stale', 'origin changed during materialization') |
| [867](../../../../src/integration/hierarchy.py#L867) | HierarchyError('invalid', f'unsupported hierarchy mutation: {mutation}') |
| [870](../../../../src/integration/hierarchy.py#L870) | HierarchyError('invalid', 'reparent requires parent_id') |
| [878](../../../../src/integration/hierarchy.py#L878) | HierarchyError('invalid', 'reparent requires a different existing parent') |
| [881](../../../../src/integration/hierarchy.py#L881) | HierarchyError('invalid', 'new parent belongs to another project') |
| [889](../../../../src/integration/hierarchy.py#L889) | HierarchyError('stale_parent', 'old parent generation changed') |
| [891](../../../../src/integration/hierarchy.py#L891) | HierarchyError('stale_parent', 'new parent generation changed') |
| [961](../../../../src/integration/hierarchy.py#L961) | HierarchyError('invalid', 'hierarchical integration is not enabled') |
| [966](../../../../src/integration/hierarchy.py#L966) | HierarchyError('invalid', 'designated repository is not in the project') |
| [998](../../../../src/integration/hierarchy.py#L998) | HierarchyError('invalid', 'hierarchical integration is not enabled') |
| [1001](../../../../src/integration/hierarchy.py#L1001) | HierarchyError('invalid', 'task is not bound to the designated repository') |
| [1004](../../../../src/integration/hierarchy.py#L1004) | HierarchyError('invalid', 'designated repository is not in the project') |
| [1023](../../../../src/integration/hierarchy.py#L1023) | HierarchyError('invalid', f'task not found: {task_id}') |
| [1035](../../../../src/integration/hierarchy.py#L1035) | HierarchyError('invalid', f'task has no integration checkpoint: {task_id}') |
| [1049](../../../../src/integration/hierarchy.py#L1049) | HierarchyError('invalid', 'task has no live branch origin') |
| [1069](../../../../src/integration/hierarchy.py#L1069) | HierarchyError('invalid', 'designated repository is not in the project') |
| [1097](../../../../src/integration/hierarchy.py#L1097) | HierarchyError('invalid', 'task hierarchy crosses the selected project') |
| [1114](../../../../src/integration/hierarchy.py#L1114) | HierarchyError('invalid', 'task is bound to a different repository') |
| [1120](../../../../src/integration/hierarchy.py#L1120) | HierarchyError('busy', 'task has an active assignment or claim') |
| [1133](../../../../src/integration/hierarchy.py#L1133) | HierarchyError('invalid', 'task already has a live branch origin and is not an unmaterialized rollout task') |
| [1146](../../../../src/integration/hierarchy.py#L1146) | HierarchyError('invalid', 'task already has an integration checkpoint') |
| [1226](../../../../src/integration/hierarchy.py#L1226) | HierarchyError('invalid', 'repository head resolver is unavailable') |
| [1231](../../../../src/integration/hierarchy.py#L1231) | HierarchyError('invalid', 'repository head is not an exact Git OID') |
| [1246](../../../../src/integration/hierarchy.py#L1246) | HierarchyError('invalid', 'branch origin base is not an exact Git OID') |
| [1249](../../../../src/integration/hierarchy.py#L1249) | HierarchyError('invalid', 'child delivery cannot target the default branch') |
| [1331](../../../../src/integration/hierarchy.py#L1331) | HierarchyError('stale_parent', 'parent generation changed') |
| [1381](../../../../src/integration/hierarchy.py#L1381) | HierarchyError('invalid', 'unknown child fields: ' + ', '.join(sorted(unknown))) |
| [1384](../../../../src/integration/hierarchy.py#L1384) | HierarchyError('invalid', 'child title is required') |
| [1401](../../../../src/integration/hierarchy.py#L1401) | HierarchyError('delivery_target_fixed', 'branch origin is already materialized') |
| [1404](../../../../src/integration/hierarchy.py#L1404) | HierarchyError('delivery_target_fixed', 'task already started') |
| [1440](../../../../src/integration/hierarchy.py#L1440) | HierarchyError('delivery_target_fixed', 'task has started or delivered work') |
| [1442](../../../../src/integration/hierarchy.py#L1442) | HierarchyError('sealed', 'task belongs to an active sealed batch') |

## src/integration/main_promotion.py

| Line | Explicit refusal |
|---|---|
| [233](../../../../src/integration/main_promotion.py#L233) | RootPromotionInvariantError('nonempty root batch has no members') |
| [339](../../../../src/integration/main_promotion.py#L339) | RootPromotionInvariantError('root batch did not enter promotion atomically') |
| [345](../../../../src/integration/main_promotion.py#L345) | RootPromotionInvariantError('root promotion reservation raced without canonical state') |
| [371](../../../../src/integration/main_promotion.py#L371) | RootPromotionInvariantError('root promotion intent does not exist') |
| [411](../../../../src/integration/main_promotion.py#L411) | RootPromotionInvariantError('root promotion App repository is not canonical') |
| [435](../../../../src/integration/main_promotion.py#L435) | RootPromotionInvariantError('root main mutation claim is missing') |
| [655](../../../../src/integration/main_promotion.py#L655) | RootPromotionInvariantError('root main proof identity changed') |
| [658](../../../../src/integration/main_promotion.py#L658) | RootPromotionInvariantError('root main proof identity changed') |
| [660](../../../../src/integration/main_promotion.py#L660) | RootPromotionInvariantError('root main write was not proven') |
| [743](../../../../src/integration/main_promotion.py#L743) | RootPromotionInvariantError('attempted root promotion cannot be superseded') |
| [765](../../../../src/integration/main_promotion.py#L765) | RootPromotionInvariantError('attempted root promotion cannot be superseded') |
| [780](../../../../src/integration/main_promotion.py#L780) | RootPromotionInvariantError('moved-main batch could not re-enter candidate building') |
| [817](../../../../src/integration/main_promotion.py#L817) | RootPromotionInvariantError('root promotion intent disappeared') |
| [1047](../../../../src/integration/main_promotion.py#L1047) | RootPromotionInvariantError('root finalization proof is incomplete') |
| [1080](../../../../src/integration/main_promotion.py#L1080) | RootPromotionInvariantError('root finalization requires the complete frozen member set') |
| [1121](../../../../src/integration/main_promotion.py#L1121) | RootPromotionInvariantError('root finalization requires the complete frozen member set') |
| [1129](../../../../src/integration/main_promotion.py#L1129) | RootPromotionInvariantError('root receipt identity changed') |
| [1206](../../../../src/integration/main_promotion.py#L1206) | RootPromotionInvariantError('root finalization terminal CAS failed') |
| [1233](../../../../src/integration/main_promotion.py#L1233) | RootPromotionInvariantError('root finalization terminal CAS failed') |
| [1310](../../../../src/integration/main_promotion.py#L1310) | RootPromotionInvariantError('authenticated main import changed identity') |
| [1555](../../../../src/integration/main_promotion.py#L1555) | RootPromotionInvariantError('candidate recovery repository is unavailable') |
| [1560](../../../../src/integration/main_promotion.py#L1560) | RootPromotionInvariantError('candidate recovery ref could not be pinned') |
| [1571](../../../../src/integration/main_promotion.py#L1571) | RootPromotionInvariantError('promotion repository is unavailable') |
| [1584](../../../../src/integration/main_promotion.py#L1584) | ValueError('root promotion authenticated repository binding changed') |
| [1627](../../../../src/integration/main_promotion.py#L1627) | RootPromotionInvariantError('root promotion identity changed') |

## src/integration/models.py

| Line | Explicit refusal |
|---|---|
| [122](../../../../src/integration/models.py#L122) | ValueError('route artifact belongs to another playbook') |
| [124](../../../../src/integration/models.py#L124) | ValueError('integration routes support only system or project scope') |
| [126](../../../../src/integration/models.py#L126) | ValueError('system integration routes require an empty scope identifier') |
| [128](../../../../src/integration/models.py#L128) | ValueError('project integration routes require a scope identifier') |
| [166](../../../../src/integration/models.py#L166) | ValueError('retry_max_seconds must be at least retry_base_seconds') |

## src/integration/outbox.py

| Line | Explicit refusal |
|---|---|
| [56](../../../../src/integration/outbox.py#L56) | ValueError('an empty integration destination manifest is not frozen') |
| [97](../../../../src/integration/outbox.py#L97) | RuntimeError('integration destination manifest lost its row lock') |
| [103](../../../../src/integration/outbox.py#L103) | DestinationArtifactUnavailable(event_id) |
| [111](../../../../src/integration/outbox.py#L111) | ValueError('integration acceptance cursor cannot regress') |
| [137](../../../../src/integration/outbox.py#L137) | ValueError('event_id, dedup_key, project_id, and event_type are required') |
| [140](../../../../src/integration/outbox.py#L140) | ValueError('payload project_id does not match the outbox project') |
| [142](../../../../src/integration/outbox.py#L142) | ValueError('payload event_id does not match the outbox event') |
| [174](../../../../src/integration/outbox.py#L174) | ValueError('integration event identity was reused with different content') |
| [190](../../../../src/integration/outbox.py#L190) | ValueError('page_size must be positive') |
| [192](../../../../src/integration/outbox.py#L192) | ValueError('retry delays must be positive') |

## src/integration/ownership.py

| Line | Explicit refusal |
|---|---|
| [102](../../../../src/integration/ownership.py#L102) | BranchBusy('branch acquisition raced another owner') |
| [107](../../../../src/integration/ownership.py#L107) | BranchBusy('branch acquisition could not resolve its owner') |
| [113](../../../../src/integration/ownership.py#L113) | BranchBusy('branch handoff is awaiting termination evidence') |
| [115](../../../../src/integration/ownership.py#L115) | BranchBusy('branch has an active or unresolved owner') |
| [134](../../../../src/integration/ownership.py#L134) | BranchBusy('branch has a live external mutation claim') |
| [138](../../../../src/integration/ownership.py#L138) | BranchBusy('attached owner lacks session/workspace handoff evidence') |
| [147](../../../../src/integration/ownership.py#L147) | BranchBusy('branch ownership state is not transferable') |
| [151](../../../../src/integration/ownership.py#L151) | BranchBusy('no server-side handoff confirmer is installed') |
| [156](../../../../src/integration/ownership.py#L156) | BranchBusy('previous writer has not confirmed stopped and detached') |
| [169](../../../../src/integration/ownership.py#L169) | BranchBusy('branch handoff changed while confirmation ran') |
| [179](../../../../src/integration/ownership.py#L179) | BranchBusy('attached owner lacks session/workspace handoff evidence') |
| [190](../../../../src/integration/ownership.py#L190) | BranchBusy('branch handoff changed while reserving confirmation') |
| [193](../../../../src/integration/ownership.py#L193) | BranchBusy('branch ownership state is not transferable') |
| [196](../../../../src/integration/ownership.py#L196) | BranchBusy('no server-side handoff confirmer is installed') |
| [201](../../../../src/integration/ownership.py#L201) | BranchBusy('previous writer has not confirmed stopped and detached') |
| [217](../../../../src/integration/ownership.py#L217) | BranchBusy('branch release evidence changed while confirming handoff') |
| [223](../../../../src/integration/ownership.py#L223) | BranchBusy('branch handoff changed while confirmation ran') |
| [225](../../../../src/integration/ownership.py#L225) | BranchBusy('branch handoff changed while confirmation ran') |
| [255](../../../../src/integration/ownership.py#L255) | BranchBusy('branch handoff changed after confirmation') |
| [264](../../../../src/integration/ownership.py#L264) | BranchBusy('branch has a live external mutation claim') |
| [275](../../../../src/integration/ownership.py#L275) | BranchBusy('branch still has an attached writer') |
| [282](../../../../src/integration/ownership.py#L282) | BranchBusy('branch has a live external mutation claim') |
| [291](../../../../src/integration/ownership.py#L291) | BranchBusy(f'branch ownership role must be {expected_role}') |
| [293](../../../../src/integration/ownership.py#L293) | BranchBusy('branch ownership is not write-authoritative') |
| [329](../../../../src/integration/ownership.py#L329) | BranchBusy(f'branch ownership role must be {expected_role}') |
| [331](../../../../src/integration/ownership.py#L331) | BranchBusy(f'branch ownership must be {state} for this mutation') |
| [346](../../../../src/integration/ownership.py#L346) | ValueError('branch attachment requires session and workspace ids') |
| [379](../../../../src/integration/ownership.py#L379) | BranchBusy(f'branch ownership role must be {expected_role}') |
| [383](../../../../src/integration/ownership.py#L383) | BranchBusy('branch is already attached to another writer') |
| [385](../../../../src/integration/ownership.py#L385) | BranchBusy('branch ownership is not reserved for attachment') |
| [398](../../../../src/integration/ownership.py#L398) | BranchBusy('workspace is no longer locked by the branch owner') |
| [414](../../../../src/integration/ownership.py#L414) | StaleFence('branch attachment lost its compare-and-swap') |
| [482](../../../../src/integration/ownership.py#L482) | StaleFence('branch owner changed while transferring') |
| [498](../../../../src/integration/ownership.py#L498) | ValueError('branch ownership target must name repository and branch') |
| [500](../../../../src/integration/ownership.py#L500) | ValueError('branch ownership requires non-empty owner and role') |
| [513](../../../../src/integration/ownership.py#L513) | StaleFence('branch ownership record does not exist') |
| [515](../../../../src/integration/ownership.py#L515) | StaleFence('branch ownership fence is stale') |

## src/integration/parent_ci.py

| Line | Explicit refusal |
|---|---|
| [25](../../../../src/integration/parent_ci.py#L25) | RuntimeError(result.stderr or 'parent CI store initialization failed') |

## src/integration/parent_completion.py

| Line | Explicit refusal |
|---|---|
| [70](../../../../src/integration/parent_completion.py#L70) | HierarchyError('invariant_error', 'checkpoint episode has no operation') |
| [75](../../../../src/integration/parent_completion.py#L75) | HierarchyError('invalid', 'hierarchical integration policy is missing') |
| [79](../../../../src/integration/parent_completion.py#L79) | HierarchyError('invalid', f'hierarchical integration policy is invalid: {exc}') |
| [90](../../../../src/integration/parent_completion.py#L90) | HierarchyError('invalid', 'parent route artifact is not stored') |
| [92](../../../../src/integration/parent_completion.py#L92) | HierarchyError('invalid', 'parent route artifact identity changed') |
| [98](../../../../src/integration/parent_completion.py#L98) | HierarchyError('invalid', 'parent route does not match the stored artifact') |
| [196](../../../../src/integration/parent_completion.py#L196) | HierarchyError('stale_head', 'previous verified aggregate changed before rollover') |
| [329](../../../../src/integration/parent_completion.py#L329) | HierarchyError('invariant_error', 'integration-ready parent has no branch owner') |
| [387](../../../../src/integration/parent_completion.py#L387) | HierarchyError('invariant_error', 'parent episode is missing') |
| [652](../../../../src/integration/parent_completion.py#L652) | HierarchyError('invalid', 'unsupported delivery disposition') |
| [654](../../../../src/integration/parent_completion.py#L654) | HierarchyError('invalid', 'disposition evidence is required') |
| [660](../../../../src/integration/parent_completion.py#L660) | HierarchyError('invalid', 'disposition child has no parent') |
| [679](../../../../src/integration/parent_completion.py#L679) | HierarchyError('invalid', 'disposition source is not terminal at that head') |
| [689](../../../../src/integration/parent_completion.py#L689) | HierarchyError('delivery_target_fixed', 'delivered code cannot be disposed') |
| [759](../../../../src/integration/parent_completion.py#L759) | HierarchyError('invariant_error', 'disposition identity changed') |
| [973](../../../../src/integration/parent_completion.py#L973) | HierarchyError('invariant_error', 'verifier handoff is not current') |
| [979](../../../../src/integration/parent_completion.py#L979) | HierarchyError('human_required', 'operator manual pause is active') |
| [1182](../../../../src/integration/parent_completion.py#L1182) | HierarchyError('invariant_error', 'parent task does not exist') |
| [1187](../../../../src/integration/parent_completion.py#L1187) | HierarchyError('invariant_error', 'hierarchical integration is disabled') |
| [1197](../../../../src/integration/parent_completion.py#L1197) | HierarchyError('invariant_error', 'parent has no active integration episode') |
| [1209](../../../../src/integration/parent_completion.py#L1209) | HierarchyError('invariant_error', 'parent episode operation is missing') |

## src/integration/preflight.py

| Line | Explicit refusal |
|---|---|
| [60](../../../../src/integration/preflight.py#L60) | ValueError('trust content response is malformed') |
| [73](../../../../src/integration/preflight.py#L73) | ValueError('hosted variable response is malformed') |

## src/integration/promotion.py

| Line | Explicit refusal |
|---|---|
| [141](../../../../src/integration/promotion.py#L141) | PromotionConflict(value, existing.get('conflict_diagnostics') or {}) |
| [147](../../../../src/integration/promotion.py#L147) | PromotionInvariantError('repository and task projects do not match') |
| [160](../../../../src/integration/promotion.py#L160) | PromotionSourceMoved('reviewed source tree does not match trusted evidence') |
| [216](../../../../src/integration/promotion.py#L216) | PromotionTargetMoved(str(exc)) |
| [217](../../../../src/integration/promotion.py#L217) | PromotionInvariantError(str(exc)) |
| [222](../../../../src/integration/promotion.py#L222) | PromotionConflict(value, intent.get('conflict_diagnostics') or {}) |
| [239](../../../../src/integration/promotion.py#L239) | PromotionConflict(self._value(intent), diagnostics) |
| [241](../../../../src/integration/promotion.py#L241) | PromotionRuntimeError((result.stderr or result.stdout or 'git merge-tree failed').strip()) |
| [270](../../../../src/integration/promotion.py#L270) | PromotionRuntimeError((commit.stderr or commit.stdout or 'git commit-tree failed').strip()) |
| [290](../../../../src/integration/promotion.py#L290) | PromotionConflict(self._value(intent), intent.get('conflict_diagnostics') or {}) |
| [295](../../../../src/integration/promotion.py#L295) | PromotionTargetMoved('push fence targets another branch') |
| [309](../../../../src/integration/promotion.py#L309) | PromotionRuntimeError(remote.error or 'target remote state is unknown') |
| [311](../../../../src/integration/promotion.py#L311) | PromotionTargetMoved('target branch is absent') |
| [315](../../../../src/integration/promotion.py#L315) | PromotionTargetMoved('target branch moved from the prepared old tip') |
| [336](../../../../src/integration/promotion.py#L336) | PromotionRuntimeError(str(exc)) |
| [356](../../../../src/integration/promotion.py#L356) | PromotionInvariantError(f'invalid {label} OID') |
| [358](../../../../src/integration/promotion.py#L358) | PromotionInvariantError('repair commit range contains duplicates') |
| [360](../../../../src/integration/promotion.py#L360) | PromotionInvariantError('repair commit range must end at resolved head') |
| [363](../../../../src/integration/promotion.py#L363) | PromotionAuthorizationError('conflict resolution requires a repair session') |
| [369](../../../../src/integration/promotion.py#L369) | PromotionAuthorizationError('repair session identity is incomplete') |
| [373](../../../../src/integration/promotion.py#L373) | PromotionInvariantError('resolution operation does not match original intent') |
| [378](../../../../src/integration/promotion.py#L378) | PromotionInvariantError('resolution fence targets another branch') |
| [410](../../../../src/integration/promotion.py#L410) | PromotionTargetMoved('repair resolution authority is stale') |
| [453](../../../../src/integration/promotion.py#L453) | PromotionAuthorizationError('resolution recovery requires LOCAL operator authority') |
| [458](../../../../src/integration/promotion.py#L458) | PromotionInvariantError('superseded resolution has no recovery successor') |
| [461](../../../../src/integration/promotion.py#L461) | PromotionInvariantError('only a reserved conflict resolution can be recovered') |
| [463](../../../../src/integration/promotion.py#L463) | PromotionInvariantError('resolution push may have started; recovery is ambiguous') |
| [465](../../../../src/integration/promotion.py#L465) | PromotionInvariantError('resolution push already has durable evidence') |
| [473](../../../../src/integration/promotion.py#L473) | PromotionTargetMoved('resolution writer ownership is absent') |
| [488](../../../../src/integration/promotion.py#L488) | PromotionTargetMoved('resolution writer is neither the exact quiescent writer nor a coordinated retained owner') |
| [511](../../../../src/integration/promotion.py#L511) | PromotionInvariantError('exact resolution writer is not quiescent') |
| [525](../../../../src/integration/promotion.py#L525) | PromotionInvariantError('resolution push may have started; recovery is ambiguous') |
| [527](../../../../src/integration/promotion.py#L527) | PromotionInvariantError('resolution push already has durable evidence') |
| [535](../../../../src/integration/promotion.py#L535) | PromotionRuntimeError(remote.error or 'target remote state is unknown') |
| [537](../../../../src/integration/promotion.py#L537) | PromotionTargetMoved('target branch is absent') |
| [539](../../../../src/integration/promotion.py#L539) | PromotionTargetMoved('target branch is not the exact reserved old tip') |
| [554](../../../../src/integration/promotion.py#L554) | PromotionAuthorizationError('resolution push requires a repair session') |
| [560](../../../../src/integration/promotion.py#L560) | PromotionAuthorizationError('repair session identity is incomplete') |
| [563](../../../../src/integration/promotion.py#L563) | PromotionInvariantError('promotion has no reserved conflict resolution') |
| [568](../../../../src/integration/promotion.py#L568) | PromotionTargetMoved('resolution push fence targets another branch') |
| [593](../../../../src/integration/promotion.py#L593) | PromotionRuntimeError(remote.error or 'target remote state is unknown') |
| [597](../../../../src/integration/promotion.py#L597) | PromotionTargetMoved('target branch is absent') |
| [605](../../../../src/integration/promotion.py#L605) | PromotionTargetMoved('target branch moved from the resolution old tip') |
| [609](../../../../src/integration/promotion.py#L609) | PromotionInvariantError('resolution push may already be in flight; reconcile it first') |
| [634](../../../../src/integration/promotion.py#L634) | PromotionRuntimeError(remote.error or 'target remote state is unknown') |
| [636](../../../../src/integration/promotion.py#L636) | PromotionTargetMoved('target branch is absent') |
| [640](../../../../src/integration/promotion.py#L640) | PromotionTargetMoved('target branch moved from the resolution old tip') |
| [651](../../../../src/integration/promotion.py#L651) | PromotionRuntimeError(str(exc)) |
| [685](../../../../src/integration/promotion.py#L685) | PromotionTargetMoved('repair resolution push authority is stale') |
| [711](../../../../src/integration/promotion.py#L711) | PromotionConflict(self._value(intent), intent.get('conflict_diagnostics') or {}) |
| [715](../../../../src/integration/promotion.py#L715) | PromotionInvariantError('promotion intent has no prepared commit') |
| [723](../../../../src/integration/promotion.py#L723) | PromotionRuntimeError(remote.error or 'target remote state is unknown') |
| [725](../../../../src/integration/promotion.py#L725) | PromotionInvariantError('target branch disappeared during reconciliation') |
| [727](../../../../src/integration/promotion.py#L727) | PromotionNotApplied('prepared push has not been applied') |
| [729](../../../../src/integration/promotion.py#L729) | PromotionInvariantError('target diverged from the prepared promotion') |
| [740](../../../../src/integration/promotion.py#L740) | PromotionRuntimeError(remote.error or 'target remote state is unknown') |
| [742](../../../../src/integration/promotion.py#L742) | PromotionInvariantError('target branch disappeared during reconciliation') |
| [744](../../../../src/integration/promotion.py#L744) | PromotionNotApplied('reserved resolution push has not been applied') |
| [746](../../../../src/integration/promotion.py#L746) | PromotionInvariantError('target diverged from the reserved resolution') |
| [760](../../../../src/integration/promotion.py#L760) | PromotionRuntimeError((fetch.stderr or 'target fetch failed').strip()) |
| [768](../../../../src/integration/promotion.py#L768) | PromotionRuntimeError('target moved while resolution was fetched') |
| [786](../../../../src/integration/promotion.py#L786) | PromotionInvariantError('resolution head is not descended from expected target') |
| [789](../../../../src/integration/promotion.py#L789) | PromotionInvariantError('resolution tree does not match reservation') |
| [794](../../../../src/integration/promotion.py#L794) | PromotionInvariantError('resolution commit range does not match reservation') |
| [808](../../../../src/integration/promotion.py#L808) | PromotionRuntimeError((lineage.stderr or 'resolution lineage scan failed').strip()) |
| [814](../../../../src/integration/promotion.py#L814) | PromotionInvariantError('resolution first-parent chain changed its target') |
| [817](../../../../src/integration/promotion.py#L817) | PromotionInvariantError('resolution contains an unreviewed merge commit') |
| [821](../../../../src/integration/promotion.py#L821) | PromotionInvariantError('resolution first-parent chain is incomplete') |
| [834](../../../../src/integration/promotion.py#L834) | PromotionRuntimeError((result.stderr or 'commit range failed').strip()) |
| [844](../../../../src/integration/promotion.py#L844) | PromotionSourceMoved(f'invalid {label} OID') |
| [847](../../../../src/integration/promotion.py#L847) | PromotionSourceMoved('source task has no materialized parent delivery identity') |
| [855](../../../../src/integration/promotion.py#L855) | PromotionSourceMoved('source task parent identity is invalid') |
| [858](../../../../src/integration/promotion.py#L858) | PromotionSourceMoved("promotion target is not the source task's immediate parent") |
| [865](../../../../src/integration/promotion.py#L865) | PromotionSourceMoved("target repository is not the task's parent repository") |
| [890](../../../../src/integration/promotion.py#L890) | PromotionSourceMoved('task branch origin is absent, moved, or retired') |
| [901](../../../../src/integration/promotion.py#L901) | PromotionSourceMoved('trusted review evidence is absent or superseded') |
| [917](../../../../src/integration/promotion.py#L917) | PromotionInvariantError('canonical repository is not configured') |
| [922](../../../../src/integration/promotion.py#L922) | PromotionInvariantError('linked repository source path is unavailable') |
| [927](../../../../src/integration/promotion.py#L927) | PromotionInvariantError('linked repository origin is unavailable') |
| [930](../../../../src/integration/promotion.py#L930) | PromotionInvariantError('canonical repository has no immutable origin') |
| [950](../../../../src/integration/promotion.py#L950) | PromotionRuntimeError((result.stderr or result.stdout or 'retained clone failed').strip()) |
| [971](../../../../src/integration/promotion.py#L971) | PromotionInvariantError('retained repository identity changed') |
| [981](../../../../src/integration/promotion.py#L981) | PromotionRuntimeError((result.stderr or 'git fetch failed').strip()) |
| [986](../../../../src/integration/promotion.py#L986) | PromotionRuntimeError(result.error or 'source remote state is unknown') |
| [988](../../../../src/integration/promotion.py#L988) | PromotionSourceMoved('source branch moved from the reviewed head') |
| [993](../../../../src/integration/promotion.py#L993) | PromotionRuntimeError(result.error or 'target remote state is unknown') |
| [995](../../../../src/integration/promotion.py#L995) | PromotionTargetMoved('target branch moved from the expected tip') |
| [1001](../../../../src/integration/promotion.py#L1001) | PromotionSourceMoved('reviewed source is not descended from its recorded base') |
| [1003](../../../../src/integration/promotion.py#L1003) | PromotionTargetMoved('expected target is not descended from the source base') |
| [1013](../../../../src/integration/promotion.py#L1013) | PromotionRuntimeError((result.stderr or 'ancestry check failed').strip()) |
| [1021](../../../../src/integration/promotion.py#L1021) | PromotionSourceMoved(f'promotion input is not a {expected_type} object') |
| [1032](../../../../src/integration/promotion.py#L1032) | PromotionSourceMoved('reviewed source tree cannot be resolved') |
| [1043](../../../../src/integration/promotion.py#L1043) | PromotionRuntimeError((result.stderr or 'git log failed').strip()) |
| [1094](../../../../src/integration/promotion.py#L1094) | PromotionInvariantError('playbook invocation artifact does not match the frozen operation route') |
| [1103](../../../../src/integration/promotion.py#L1103) | PromotionInvariantError('reviewer session attempt is unavailable') |
| [1155](../../../../src/integration/promotion.py#L1155) | PromotionInvariantError('promotion intent identity changed: ' + ', '.join(changed)) |
| [1163](../../../../src/integration/promotion.py#L1163) | PromotionInvariantError('clean merge-tree output was not one tree OID') |
| [1228](../../../../src/integration/promotion.py#L1228) | PromotionInvariantError('recovery ref points to another prepared commit') |
| [1237](../../../../src/integration/promotion.py#L1237) | PromotionRuntimeError((result.stderr or 'recovery ref update failed').strip()) |
| [1252](../../../../src/integration/promotion.py#L1252) | PromotionRuntimeError((fetch.stderr or 'target fetch failed').strip()) |
| [1260](../../../../src/integration/promotion.py#L1260) | PromotionRuntimeError('target moved while reconciliation fetched it') |
| [1267](../../../../src/integration/promotion.py#L1267) | PromotionInvariantError('root intent requires the root-only finalizer') |
| [1279](../../../../src/integration/promotion.py#L1279) | PromotionInvariantError('promotion intent does not exist') |
| [1288](../../../../src/integration/promotion.py#L1288) | PromotionInvariantError('promotion repository identity changed') |
| [1290](../../../../src/integration/promotion.py#L1290) | PromotionInvariantError('retained promotion repository is unavailable') |
| [1298](../../../../src/integration/promotion.py#L1298) | PromotionInvariantError('promotion repository identity changed') |

## src/integration/recovery_controls.py

| Line | Explicit refusal |
|---|---|
| [172](../../../../src/integration/recovery_controls.py#L172) | RuntimeError('repair continuation changed during resume') |
| [254](../../../../src/integration/recovery_controls.py#L254) | RuntimeError('legacy resolution changed during resume') |
| [259](../../../../src/integration/recovery_controls.py#L259) | RuntimeError('parent recovery changed during resume') |
| [299](../../../../src/integration/recovery_controls.py#L299) | RuntimeError('repair continuation changed during human resume') |
| [306](../../../../src/integration/recovery_controls.py#L306) | RuntimeError('delegate recovery changed during resume') |
| [561](../../../../src/integration/recovery_controls.py#L561) | ValueError('abort reason is required') |
| [714](../../../../src/integration/recovery_controls.py#L714) | ValueError('operation target has no owning project') |

## src/integration/release.py

| Line | Explicit refusal |
|---|---|
| [290](../../../../src/integration/release.py#L290) | _CASLost |

## src/integration/repair.py

| Line | Explicit refusal |
|---|---|
| [101](../../../../src/integration/repair.py#L101) | ValueError('integration batch does not exist') |
| [116](../../../../src/integration/repair.py#L116) | ValueError('batch repair operation identity conflicts') |
| [129](../../../../src/integration/repair.py#L129) | ValueError('batch is outside enabled hierarchical integration scope') |
| [147](../../../../src/integration/repair.py#L147) | ValueError('batch route artifact identity is not stored and frozen') |
| [674](../../../../src/integration/repair.py#L674) | RuntimeError('repair continuation lost its stage compare-and-swap') |
| [961](../../../../src/integration/repair.py#L961) | RuntimeError('repair stage writer changed while linking delegate') |
| [1545](../../../../src/integration/repair.py#L1545) | ValueError('batch repair requires exact verified commit lineage') |
| [1558](../../../../src/integration/repair.py#L1558) | ValueError('batch repair operation is not active') |
| [1575](../../../../src/integration/repair.py#L1575) | ValueError('batch repair stage is no longer active') |
| [1577](../../../../src/integration/repair.py#L1577) | ValueError('batch repair subject changed during close') |
| [1601](../../../../src/integration/repair.py#L1601) | ValueError('batch rebuild repair must be the exact ancestry-preserving merge') |
| [1626](../../../../src/integration/repair.py#L1626) | ValueError('batch CI repair requires a fully constructed candidate') |
| [1640](../../../../src/integration/repair.py#L1640) | ValueError('batch CI repair cannot replace an unresolved member') |
| [1714](../../../../src/integration/repair.py#L1714) | ValueError('batch repair operation is not active') |
| [1727](../../../../src/integration/repair.py#L1727) | ValueError('batch repair stage is not current') |
| [1985](../../../../src/integration/repair.py#L1985) | ValueError('parent repair subject HEAD is not an exact Git OID') |
| [1999](../../../../src/integration/repair.py#L1999) | ValueError('parent repair operation is not active') |
| [2011](../../../../src/integration/repair.py#L2011) | ValueError('parent repair checkpoint identity conflicts') |
| [2023](../../../../src/integration/repair.py#L2023) | ValueError('parent repair stage is not current') |
| [2559](../../../../src/integration/repair.py#L2559) | RuntimeError('retained repair handoff lost its compare-and-swap') |
| [2702](../../../../src/integration/repair.py#L2702) | ValueError('batch repair operation identity changed') |
| [2712](../../../../src/integration/repair.py#L2712) | ValueError('batch current candidate revision is missing') |
| [2871](../../../../src/integration/repair.py#L2871) | ValueError('repair commit proof does not match the current subject') |
| [3045](../../../../src/integration/repair.py#L3045) | ValueError('repair operation project identity is missing') |
| [3063](../../../../src/integration/repair.py#L3063) | _RepairInvariant('repair policy snapshot is corrupt') |
| [3065](../../../../src/integration/repair.py#L3065) | _RepairInvariant('repair target kind is corrupt') |
| [3076](../../../../src/integration/repair.py#L3076) | _RepairInvariant('repair frozen route identity is corrupt') |
| [3081](../../../../src/integration/repair.py#L3081) | _RepairInvariant(str(exc)) |
| [3089](../../../../src/integration/repair.py#L3089) | _RepairInvariant('batch repair project identity is corrupt') |
| [3095](../../../../src/integration/repair.py#L3095) | _RepairInvariant('batch repair identity is corrupt') |
| [3109](../../../../src/integration/repair.py#L3109) | _RepairInvariant('parent repair target is missing') |
| [3114](../../../../src/integration/repair.py#L3114) | _RepairInvariant('parent repair project identity is corrupt') |
| [3123](../../../../src/integration/repair.py#L3123) | _RepairInvariant('parent repair checkpoint identity is corrupt') |

## src/integration/review_evidence.py

| Line | Explicit refusal |
|---|---|
| [49](../../../../src/integration/review_evidence.py#L49) | HierarchyError('invalid', 'review verdict is invalid') |
| [59](../../../../src/integration/review_evidence.py#L59) | HierarchyError('unauthorized', 'reviewer session identity is not live') |
| [68](../../../../src/integration/review_evidence.py#L68) | HierarchyError('unauthorized', 'review target is not graph-derived') |
| [70](../../../../src/integration/review_evidence.py#L70) | HierarchyError('unauthorized', 'review subject belongs to another project') |
| [75](../../../../src/integration/review_evidence.py#L75) | HierarchyError('unauthorized', 'reviewer session attempt is not current') |
| [79](../../../../src/integration/review_evidence.py#L79) | HierarchyError('invalid', 'review subject is not in the designated repository') |
| [83](../../../../src/integration/review_evidence.py#L83) | HierarchyError('invalid', 'review subject has no exact integration snapshot') |
| [92](../../../../src/integration/review_evidence.py#L92) | HierarchyError('invalid', 'parent aggregate is not currently verified') |
| [120](../../../../src/integration/review_evidence.py#L120) | HierarchyError('invalid', 'review repository project changed') |
| [128](../../../../src/integration/review_evidence.py#L128) | HierarchyError('stale_head', 'reviewed remote ref is not the exact head') |
| [179](../../../../src/integration/review_evidence.py#L179) | HierarchyError('unauthorized', 'review evidence task identity changed') |
| [192](../../../../src/integration/review_evidence.py#L192) | HierarchyError('invalid', 'rejection subject changed') |
| [209](../../../../src/integration/review_evidence.py#L209) | HierarchyError('invariant_error', 'review evidence identity changed') |
| [216](../../../../src/integration/review_evidence.py#L216) | HierarchyError('stale_head', 'review task disappeared before verdict commit') |
| [334](../../../../src/integration/review_evidence.py#L334) | HierarchyError('stale_head', 'review snapshot changed before verdict commit') |
| [341](../../../../src/integration/review_evidence.py#L341) | HierarchyError('unauthorized', 'reviewer has no unique graph subject') |
| [344](../../../../src/integration/review_evidence.py#L344) | HierarchyError('invalid', 'review subject is missing') |
| [353](../../../../src/integration/review_evidence.py#L353) | HierarchyError('unauthorized', 'final review graph is ambiguous') |
| [356](../../../../src/integration/review_evidence.py#L356) | HierarchyError('invalid', 'final review subject is missing') |
| [365](../../../../src/integration/review_evidence.py#L365) | HierarchyError('unauthorized', 'final review has no unique integration subject') |

## src/integration/scheduler.py

| Line | Explicit refusal |
|---|---|
| [51](../../../../src/integration/scheduler.py#L51) | ValueError('integration schedule interval must be positive') |
| [73](../../../../src/integration/scheduler.py#L73) | ValueError('integration schedule trigger must be periodic or manual') |
| [320](../../../../src/integration/scheduler.py#L320) | ValueError('integration train page size must be positive') |
| [327](../../../../src/integration/scheduler.py#L327) | ValueError('integration seal project and request are required') |
| [355](../../../../src/integration/scheduler.py#L355) | ValueError('integration seal request is not outstanding') |
| [368](../../../../src/integration/scheduler.py#L368) | ValueError('project is not configured for integration trains') |
| [379](../../../../src/integration/scheduler.py#L379) | ValueError('designated integration repository does not exist') |
| [401](../../../../src/integration/scheduler.py#L401) | ValueError('root route artifact identity is not stored and exact') |
| [416](../../../../src/integration/scheduler.py#L416) | ValueError('expired integration lease has no resumable batch') |
| [460](../../../../src/integration/scheduler.py#L460) | ValueError('integration source refs must be unique within a batch') |
| [470](../../../../src/integration/scheduler.py#L470) | ValueError('expired non-empty sealing batch lost its frontier') |
| [723](../../../../src/integration/scheduler.py#L723) | ValueError('integration seal request changed during sealing') |
| [744](../../../../src/integration/scheduler.py#L744) | ValueError('integration source branch identity is invalid') |

## src/integration/service.py

| Line | Explicit refusal |
|---|---|
| [40](../../../../src/integration/service.py#L40) | ValueError('integration service page size must be positive') |
| [42](../../../../src/integration/service.py#L42) | ValueError('integration service interval must be positive') |

## src/integration/status.py

| Line | Explicit refusal |
|---|---|
| [74](../../../../src/integration/status.py#L74) | RuntimeError('database is not initialized') |

## src/orchestrator/git_ops.py

No explicit raise sites; eligibility/results may still block actions.

## src/config.py

| Line | Explicit refusal |
|---|---|
| [1898](../../../../src/config.py#L1898) | ConfigValidationError([f'[integration] {name}: must be a mapping of non-secret references']) |
| [1902](../../../../src/config.py#L1902) | ConfigValidationError([f'[integration] {name}: contains unsupported credential fields']) |
| [1926](../../../../src/config.py#L1926) | ConfigValidationError([f'[integration] {name}: contains an unsafe identity or reference']) |
| [3020](../../../../src/config.py#L3020) | ValueError(f'Environment variable {var_name} not set') |
| [3263](../../../../src/config.py#L3263) | FileNotFoundError(f'Config file not found: {path}') |
| [3303](../../../../src/config.py#L3303) | FileNotFoundError(msg) |
| [3854](../../../../src/config.py#L3854) | ConfigValidationError(fatal_errors) |

## Database guard functions

Baseline guard functions; later revisions can replace their definitions.

- [integration_member_is_mutable](../../../../migrations/integration_guards.py#L24)
- [integration_checkpoint_is_monotone](../../../../migrations/integration_guards.py#L44)
- [integration_batch_revision_is_monotone](../../../../migrations/integration_guards.py#L53)
- [integration_branch_fence_is_monotone](../../../../migrations/integration_guards.py#L62)
- [integration_lease_fence_is_monotone](../../../../migrations/integration_guards.py#L71)
- [integration_schedule_sequence_is_monotone](../../../../migrations/integration_guards.py#L80)
- [integration_outbox_attempts_monotone](../../../../migrations/integration_guards.py#L89)
- [integration_repair_attempts_monotone](../../../../migrations/integration_guards.py#L98)
- [integration_candidate_progress_monotone](../../../../migrations/integration_guards.py#L109)
- [integration_repair_operation_stage_monotone](../../../../migrations/integration_guards.py#L118)
- [integration_prepared_identity_immutable](../../../../migrations/integration_guards.py#L127)
- [task_delivery_receipt_append_only](../../../../migrations/integration_guards.py#L131)
- [task_branch_origin_materialized_immutable](../../../../migrations/integration_guards.py#L138)
- [integration_outbox_cursor_monotone](../../../../migrations/integration_guards.py#L148)
- [integration_review_evidence_append_only](../../../../migrations/integration_guards.py#L152)
- [integration_parent_audit_append_only](../../../../migrations/integration_guards.py#L156)
- [integration_batch_identity_is_immutable](../../../../migrations/integration_guards.py#L160)
- [integration_empty_batch_target_rejected](../../../../migrations/integration_guards.py#L164)
- [integration_candidate_publication_is_monotone](../../../../migrations/integration_guards.py#L168)
- [integration_candidate_resolution_is_monotone](../../../../migrations/integration_guards.py#L191)
- [integration_candidate_mutation_is_monotone](../../../../migrations/integration_guards.py#L232)
- [integration_root_intent_terminal_guard](../../../../migrations/integration_guards.py#L255)
- [integration_root_member_append_only](../../../../migrations/integration_guards.py#L263)
- [integration_root_prewrite_immutable](../../../../migrations/integration_guards.py#L267)
- [integration_attestation_publication_guard](../../../../migrations/integration_guards.py#L278)
- [integration_cleanup_item_guard](../../../../migrations/integration_guards.py#L296)
- [integration_release_result_immutable](../../../../migrations/integration_guards.py#L306)
- [integration_cleanup_irreversible_guard](../../../../migrations/integration_guards.py#L312)
- [integration_control_history_immutable](../../../../migrations/integration_guards.py#L328)
