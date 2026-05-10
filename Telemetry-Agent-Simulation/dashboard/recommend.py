"""
Advisory scheduler recommendation logic.

This module does not launch jobs or schedule containers. It reads current
health scores and recent per-peer RTT paths, then returns an explainable
recommendation about which nodes look safer to prefer or avoid.
"""

RECENT_JOB_LIMIT = 5
RTT_HIGH_THRESHOLD_MS = 10.0
RTT_SEVERE_THRESHOLD_MS = 25.0
RTT_BAD_THRESHOLD_MS = RTT_HIGH_THRESHOLD_MS
MIN_HEALTH_SCORE = 0.75
TIMEOUT_RISK_WEIGHT = 0.15
HIGH_RTT_RISK_WEIGHT = 0.10
HEALTH_RISK_WEIGHT = 0.55
AVG_RTT_RISK_WEIGHT = 0.20
RISK_SEPARATION_THRESHOLD = 0.15
SELECTED_JOB_RISK_SEPARATION = 0.20
SELECTED_JOB_MIN_HIGH_PATHS = 2


EMPTY_RECOMMENDATION = {
    "recommended_nodes": [],
    "avoid_nodes": [],
    "confidence": 0.0,
    "reason": "Not enough RTT data yet.",
    "signals": {
        "health_scores": {},
        "rtt_path_scores": {},
        "high_rtt_paths": [],
        "per_node_risk": {},
        "recent_jobs_considered": [],
    },
    "selected_job_id": None,
    "high_rtt_paths": [],
    "per_node_risk": {},
    "mode": "none",
}


def _table_exists(conn, table_name):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, value))


def _recent_jobs(conn):
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT job_id
            FROM metrics
            GROUP BY job_id
            ORDER BY MAX(timestamp) DESC
            LIMIT ?
            """,
            (RECENT_JOB_LIMIT,),
        ).fetchall()
    ]


def _health_scores(conn):
    if not _table_exists(conn, "health_scores"):
        return {}

    health_rows = conn.execute(
        """
        SELECT node_id, current_score
        FROM health_scores
        """
    ).fetchall()
    return {row["node_id"]: float(row["current_score"]) for row in health_rows}


def _load_rtt_rows(conn, job_ids):
    if not job_ids:
        return []

    placeholders = ",".join("?" for _ in job_ids)
    return conn.execute(
        f"""
        SELECT node_id, peer_node_id, rtt_ms, job_id
        FROM rtt_metrics
        WHERE job_id IN ({placeholders})
        """,
        job_ids,
    ).fetchall()


def _build_path_and_node_stats(rtt_rows, health_scores):
    nodes = set(health_scores)
    for row in rtt_rows:
        nodes.add(row["node_id"])
        nodes.add(row["peer_node_id"])

    node_stats = {
        node: {
            "health_score": health_scores.get(node),
            "rtt_values": [],
            "timeout_count": 0,
            "high_rtt_path_count": 0,
            "severe_rtt_path_count": 0,
            "path_count": 0,
        }
        for node in nodes
    }

    path_scores = {}
    for row in rtt_rows:
        src = row["node_id"]
        peer = row["peer_node_id"]
        rtt = row["rtt_ms"]
        key = f"{src}->{peer}"
        path = path_scores.setdefault(key, {
            "from": src,
            "to": peer,
            "rows": 0,
            "avg_rtt_ms": None,
            "max_rtt_ms": None,
            "timeout_count": 0,
            "is_high_rtt": False,
            "is_severe_rtt": False,
            "_rtts": [],
        })
        path["rows"] += 1

        if rtt is None:
            path["timeout_count"] += 1
            node_stats[src]["timeout_count"] += 1
            node_stats[peer]["timeout_count"] += 1
            continue

        rtt = float(rtt)
        path["_rtts"].append(rtt)
        node_stats[src]["rtt_values"].append(rtt)
        node_stats[peer]["rtt_values"].append(rtt)

    high_rtt_paths = []
    for path in path_scores.values():
        values = path.pop("_rtts")
        if values:
            path["avg_rtt_ms"] = round(sum(values) / len(values), 2)
            path["max_rtt_ms"] = round(max(values), 2)
            path["is_high_rtt"] = path["avg_rtt_ms"] >= RTT_HIGH_THRESHOLD_MS
            path["is_severe_rtt"] = path["max_rtt_ms"] >= RTT_SEVERE_THRESHOLD_MS
        if path["timeout_count"] > 0:
            path["is_high_rtt"] = True
            path["is_severe_rtt"] = True

        src = path["from"]
        peer = path["to"]
        for node in (src, peer):
            node_stats[node]["path_count"] += 1
            if path["is_high_rtt"]:
                node_stats[node]["high_rtt_path_count"] += 1
            if path["is_severe_rtt"]:
                node_stats[node]["severe_rtt_path_count"] += 1

        if path["is_high_rtt"]:
            high_rtt_paths.append({
                "from": src,
                "to": peer,
                "avg_rtt_ms": path["avg_rtt_ms"],
                "max_rtt_ms": path["max_rtt_ms"],
                "timeout_count": path["timeout_count"],
                "is_severe_rtt": path["is_severe_rtt"],
            })

    return nodes, path_scores, node_stats, high_rtt_paths


def _finalize_node_risk(node_stats, selected_job_mode=False):
    max_avg_rtt = 0.0
    for stats in node_stats.values():
        values = stats["rtt_values"]
        stats["avg_rtt_ms"] = (sum(values) / len(values)) if values else None
        stats["max_rtt_ms"] = max(values) if values else None
        if stats["avg_rtt_ms"] is not None:
            max_avg_rtt = max(max_avg_rtt, stats["avg_rtt_ms"])

    max_avg_rtt = max(max_avg_rtt, RTT_HIGH_THRESHOLD_MS)
    risks = {}
    for node, stats in node_stats.items():
        health = stats["health_score"]
        health_risk = 0.5 if health is None else _clamp((MIN_HEALTH_SCORE - health) / MIN_HEALTH_SCORE)
        avg_rtt = stats["avg_rtt_ms"] or 0.0
        avg_rtt_risk = _clamp(avg_rtt / max_avg_rtt)
        path_count = max(stats["path_count"], 1)
        timeout_risk = _clamp(stats["timeout_count"] / path_count)
        high_rtt_risk = _clamp(stats["high_rtt_path_count"] / path_count)
        severe_rtt_risk = _clamp(stats["severe_rtt_path_count"] / path_count)

        if selected_job_mode:
            # For the selected job, per-peer RTT shape should dominate. Health
            # scores are historical and useful context, but they should not hide
            # a current job where one node appears on most high-latency paths.
            risk = (
                0.42 * high_rtt_risk
                + 0.23 * severe_rtt_risk
                + 0.20 * avg_rtt_risk
                + 0.10 * timeout_risk
                + 0.05 * health_risk
            )
        else:
            risk = (
                HEALTH_RISK_WEIGHT * health_risk
                + AVG_RTT_RISK_WEIGHT * avg_rtt_risk
                + TIMEOUT_RISK_WEIGHT * timeout_risk
                + HIGH_RTT_RISK_WEIGHT * high_rtt_risk
            )

        risks[node] = risk
        stats["risk_score"] = round(risk, 3)
        stats["avg_rtt_ms"] = round(stats["avg_rtt_ms"], 2) if stats["avg_rtt_ms"] is not None else None
        stats["max_rtt_ms"] = round(stats["max_rtt_ms"], 2) if stats["max_rtt_ms"] is not None else None
        stats["high_rtt_path_fraction"] = round(stats["high_rtt_path_count"] / path_count, 3)
        stats["severe_rtt_path_fraction"] = round(stats["severe_rtt_path_count"] / path_count, 3)
        stats.pop("rtt_values", None)

    return risks


def _selected_job_recommendation(selected_job_id, rtt_rows, health_scores):
    nodes, path_scores, node_stats, high_rtt_paths = _build_path_and_node_stats(rtt_rows, health_scores)
    risks = _finalize_node_risk(node_stats, selected_job_mode=True)
    ranked = sorted(risks.items(), key=lambda item: item[1], reverse=True)

    if not ranked:
        return None

    if not high_rtt_paths:
        recommended_nodes = [node for node, _ in sorted(risks.items(), key=lambda item: item[1])]
        reason = f"Selected job {selected_job_id} has no RTT paths above {RTT_HIGH_THRESHOLD_MS:.0f} ms."
        return _recommendation_payload(
            recommended_nodes=recommended_nodes,
            avoid_nodes=[],
            confidence=0.0,
            reason=reason,
            health_scores=health_scores,
            path_scores=path_scores,
            node_stats=node_stats,
            high_rtt_paths=high_rtt_paths,
            recent_jobs=[selected_job_id],
            selected_job_id=selected_job_id,
            mode="selected_job",
        )

    highest_node, highest_risk = ranked[0]
    second_risk = ranked[1][1] if len(ranked) > 1 else 0.0
    top_stats = node_stats[highest_node]
    high_path_gap = top_stats["high_rtt_path_count"] - max(
        (node_stats[node]["high_rtt_path_count"] for node in nodes if node != highest_node),
        default=0,
    )
    all_high_paths_involve_top = all(
        path["from"] == highest_node or path["to"] == highest_node
        for path in high_rtt_paths
    )
    top_has_multiple_high_paths = top_stats["high_rtt_path_count"] >= SELECTED_JOB_MIN_HIGH_PATHS

    should_avoid = (
        top_has_multiple_high_paths
        and high_path_gap > 0
        and all_high_paths_involve_top
    )

    if should_avoid:
        avoid_nodes = [highest_node]
        recommended_nodes = [
            node
            for node, _ in sorted(risks.items(), key=lambda item: item[1])
            if node not in avoid_nodes
        ]
        separation = max(highest_risk - second_risk, high_path_gap / max(top_stats["path_count"], 1))
        confidence = round(_clamp(separation / SELECTED_JOB_RISK_SEPARATION), 2)
        severe_count = top_stats["severe_rtt_path_count"]
        reason = (
            f"Selected job {selected_job_id}: {highest_node} appears on "
            f"{top_stats['high_rtt_path_count']} high-latency RTT paths"
            f"{f' including {severe_count} severe paths' if severe_count else ''}. "
            "The RTT paths between the remaining nodes look healthy, so this is the clearest node to avoid."
        )
    else:
        avoid_nodes = []
        recommended_nodes = [node for node, _ in sorted(risks.items(), key=lambda item: item[1])]
        separation = highest_risk - second_risk
        confidence = round(_clamp(separation / SELECTED_JOB_RISK_SEPARATION), 2)
        reason = (
            f"Selected job {selected_job_id}: high RTT exists, but it is not concentrated on one node "
            "strongly enough to recommend avoiding a specific node."
        )

    return _recommendation_payload(
        recommended_nodes=recommended_nodes,
        avoid_nodes=avoid_nodes,
        confidence=confidence,
        reason=reason,
        health_scores=health_scores,
        path_scores=path_scores,
        node_stats=node_stats,
        high_rtt_paths=high_rtt_paths,
        recent_jobs=[selected_job_id],
        selected_job_id=selected_job_id,
        mode="selected_job",
    )


def _recommendation_payload(
    recommended_nodes,
    avoid_nodes,
    confidence,
    reason,
    health_scores,
    path_scores,
    node_stats,
    high_rtt_paths,
    recent_jobs,
    selected_job_id=None,
    mode="historical",
):
    per_node_risk = dict(sorted(node_stats.items()))
    return {
        "recommended_nodes": recommended_nodes,
        "avoid_nodes": avoid_nodes,
        "confidence": confidence,
        "reason": reason,
        "selected_job_id": selected_job_id,
        "high_rtt_paths": high_rtt_paths,
        "per_node_risk": per_node_risk,
        "mode": mode,
        "signals": {
            "health_scores": {node: round(score, 3) for node, score in health_scores.items()},
            "rtt_path_scores": path_scores,
            "high_rtt_paths": high_rtt_paths,
            "node_risk": per_node_risk,
            "per_node_risk": per_node_risk,
            "recent_jobs_considered": recent_jobs,
            "selected_job_id": selected_job_id,
            "mode": mode,
            "thresholds": {
                "rtt_high_ms": RTT_HIGH_THRESHOLD_MS,
                "rtt_severe_ms": RTT_SEVERE_THRESHOLD_MS,
                "min_health_score": MIN_HEALTH_SCORE,
            },
        },
    }


def compute_recommendation(conn, selected_job_id=None):
    if not _table_exists(conn, "rtt_metrics"):
        return EMPTY_RECOMMENDATION.copy()

    health_scores = _health_scores(conn)

    if selected_job_id:
        selected_rows = _load_rtt_rows(conn, [selected_job_id])
        if selected_rows:
            return _selected_job_recommendation(selected_job_id, selected_rows, health_scores)

    recent_jobs = _recent_jobs(conn)
    if not recent_jobs:
        return EMPTY_RECOMMENDATION.copy()

    rtt_rows = _load_rtt_rows(conn, recent_jobs)
    if not rtt_rows:
        return EMPTY_RECOMMENDATION.copy()

    nodes, path_scores, node_stats, high_rtt_paths = _build_path_and_node_stats(rtt_rows, health_scores)
    risks = _finalize_node_risk(node_stats, selected_job_mode=False)

    ranked = sorted(risks.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return EMPTY_RECOMMENDATION.copy()

    highest_node, highest_risk = ranked[0]
    second_risk = ranked[1][1] if len(ranked) > 1 else 0.0
    separation = highest_risk - second_risk
    confidence = _clamp(separation / max(RISK_SEPARATION_THRESHOLD, 0.01))

    if highest_risk < RISK_SEPARATION_THRESHOLD or separation < RISK_SEPARATION_THRESHOLD:
        avoid_nodes = []
        recommended_nodes = [node for node, _ in sorted(risks.items(), key=lambda item: item[1])]
        reason = "No clearly degraded node detected from recent health and RTT path data."
        confidence = round(confidence, 2)
    else:
        avoid_nodes = [highest_node]
        recommended_nodes = [node for node, _ in sorted(risks.items(), key=lambda item: item[1]) if node not in avoid_nodes]
        confidence = round(confidence, 2)
        reason = (
            f"Historical view: {highest_node} has the highest recent risk from health score and per-peer RTT paths. "
            "Treat this as advisory, not an automatic scheduling decision."
        )

    return _recommendation_payload(
        recommended_nodes=recommended_nodes,
        avoid_nodes=avoid_nodes,
        confidence=confidence,
        reason=reason,
        health_scores=health_scores,
        path_scores=path_scores,
        node_stats=node_stats,
        high_rtt_paths=high_rtt_paths,
        recent_jobs=recent_jobs,
        selected_job_id=selected_job_id,
        mode="historical",
    )
