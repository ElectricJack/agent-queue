"""Pure projections of persisted wait facts; commentary never supplies evidence."""

from collections import defaultdict, deque

from src.waits.conditions import Condition


def owner_key(record):
    kind = {"scheduler": "task", "integration": "batch"}.get(
        record["owner_kind"], record["owner_kind"]
    )
    return f"{kind}:{record['owner_id']}"


def chain_from(roots, edges, *, max_nodes=1000):
    adjacency = defaultdict(list)
    for edge in edges:
        adjacency[edge["waiter_key"]].append(edge)
    seen = set(roots)
    queue = deque(sorted(seen))
    chain = []
    truncated = False
    while queue:
        node = queue.popleft()
        for edge in sorted(adjacency[node], key=lambda e: (e["blocker_key"], e["id"])):
            blocker = edge["blocker_key"]
            if blocker not in seen:
                if len(seen) >= max_nodes:
                    truncated = True
                    continue
                seen.add(blocker)
                queue.append(blocker)
            chain.append(
                {
                    "id": edge["id"],
                    "wait_id": edge["wait_id"],
                    "from": node,
                    "to": blocker,
                    "relation": edge["relation"],
                    "source_revision": edge["source_revision"],
                    "active": edge["active"],
                    "created_at": edge["created_at"],
                    "cleared_at": edge["cleared_at"],
                }
            )
    return chain, truncated


def build_wait_explanation(
    record,
    *,
    task,
    projection,
    edges,
    deadlines,
    escalations,
    reservations,
    continuation,
    now,
    history=False,
):
    inputs = record["decision_inputs"] or {}
    receipt = inputs.get("registration_receipt", {})
    evidence = receipt.get("evidence", {})
    # History follows edges alive at the latest escalation (or resolution),
    # rather than inventing a path by joining edges from different intervals.
    at = escalations[-1]["raised_at"] if escalations else record["resolved_at"] or now
    snapshot_edges = (
        [
            e
            for e in edges
            if e["created_at"] <= at and (e["cleared_at"] is None or e["cleared_at"] >= at)
        ]
        if history
        else edges
    )
    chain, truncated = chain_from({owner_key(record)}, snapshot_edges)
    return {
        "wait_id": record["id"],
        "project_id": record["project_id"],
        "owner": {"kind": record["owner_kind"], "id": record["owner_id"]},
        "lifecycle": task["status"] if task else None,
        "outcome": record["outcome"],
        "result_ref": record["result_ref"],
        "age_seconds": max(0, (record["resolved_at"] or now) - record["created_at"]),
        "age_unknown": bool(inputs.get("age_unknown", False)),
        "verified": {
            "kind": record["condition_kind"],
            "condition": Condition.from_record(record["condition"]).as_dict(),
            "state": record["verification_state"],
            "evidence": record["evidence_ref"],
            "source_key": record["source_key"],
            "source_generation": record["source_generation"],
            "source_revision": evidence.get("revision"),
            "observed_at": record["verified_at"],
            "freshness_seconds": max(0, now - record["verified_at"]),
            "facts": evidence.get("facts", {}),
        },
        "claimed": record["claimed"],
        "sleep": {
            "decision": record["sleep_decision"],
            "reason": record["decision_reason"],
            "inputs": {k: v for k, v in inputs.items() if k != "registration_receipt"},
        },
        "deadlines": [
            {
                "type": d["deadline_type"],
                "due_at": d["due_at"],
                "generation": d["generation"],
                "fired_at": d["fired_at"],
                "cancelled_at": d["cancelled_at"],
            }
            for d in deadlines
        ],
        "chain": chain,
        "chain_truncated": truncated,
        "chain_at": at if history else now,
        "edge_history": [e for e in edges if e["wait_id"] == record["id"]] if history else [],
        "effective_priority": {
            "value": task["effective_priority"] if task else None,
            "base": task["priority"] if task else None,
            "donor": f"task:{projection['donor_task_id']}"
            if projection and projection["donor_task_id"]
            else None,
            "donor_path": projection["donor_path"] if projection else [],
            "graph_version": projection["graph_version"] if projection else None,
            "computed_at": projection["computed_at"] if projection else None,
        },
        "escalation": escalations[-1] if escalations else None,
        "escalations": escalations,
        "reservations": reservations,
        "continuation": continuation,
        "policy_layer": inputs.get("policy_layer", "default"),
    }


def graph_paths(edges, *, max_path_nodes=64, limit=10):
    """Collapse strongly connected components, then find longest DAG paths.

    Iterative Kosaraju avoids Python's recursion limit for long dependency
    chains. Cycles are diagnostic facts; this read never admits unsafe work.
    """
    adjacency, reverse = defaultdict(set), defaultdict(set)
    nodes = set()
    for edge in edges:
        a, b = edge["waiter_key"], edge["blocker_key"]
        adjacency[a].add(b)
        reverse[b].add(a)
        nodes.update((a, b))
    visited, order = set(), []
    for root in sorted(nodes):
        if root in visited:
            continue
        stack = [(root, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                order.append(node)
            elif node not in visited:
                visited.add(node)
                stack.append((node, True))
                stack.extend(
                    (n, False) for n in sorted(adjacency[node], reverse=True) if n not in visited
                )
    components, membership = [], {}
    for root in reversed(order):
        if root in membership:
            continue
        number = len(components)
        component, stack = [], [root]
        while stack:
            node = stack.pop()
            if node in membership:
                continue
            membership[node] = number
            component.append(node)
            stack.extend(sorted(reverse[node], reverse=True))
        components.append(sorted(component))
    dag, incoming = defaultdict(set), defaultdict(set)
    for node in sorted(nodes):
        for blocker in adjacency[node]:
            a, b = membership[node], membership[blocker]
            if a != b:
                dag[a].add(b)
                incoming[b].add(a)
    indegree = {i: len(incoming[i]) for i in range(len(components))}
    queue = deque(sorted(i for i, degree in indegree.items() if not degree))
    lengths, paths = {}, {}
    while queue:
        component = queue.popleft()
        parents = incoming[component]
        parent = min(parents, key=lambda p: (-lengths[p], paths[p])) if parents else None
        lengths[component] = lengths[parent] + 1 if parent is not None else 1
        path = (*paths[parent], component) if parent is not None else (component,)
        paths[component] = path[:max_path_nodes]
        for child in sorted(dag[component]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    longest = sorted((i for i in paths if not dag[i]), key=lambda i: (-lengths[i], paths[i]))[
        :limit
    ]
    deadlocks = [c for c in components if len(c) > 1 or c[0] in adjacency[c[0]]]
    return {
        "longest_chains": [
            {
                "components": [components[c] for c in paths[i]],
                "length": lengths[i],
                "truncated": lengths[i] > max_path_nodes,
            }
            for i in longest
        ],
        "deadlocks": [{"code": "wait.deadlock", "nodes": c} for c in sorted(deadlocks)],
    }
