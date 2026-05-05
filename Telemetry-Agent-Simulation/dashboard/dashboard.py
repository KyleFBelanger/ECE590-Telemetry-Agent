import os
import sqlite3
import pandas as pd
import streamlit as st
import plotly.express as px

DB_PATH = "/workspace/data/metrics.db"

st.set_page_config(
    page_title="Cluster Telemetry Dashboard",
    page_icon="📡",
    layout="wide"
)

st.title("📡 Cloud Training Cluster Telemetry Dashboard")
st.caption("Monitoring node health, straggler behavior, and training communication metrics.")


def table_exists(conn, table_name):
    query = """
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name=?
    """
    return conn.execute(query, (table_name,)).fetchone() is not None


@st.cache_data(ttl=2)
def load_data():
    if not os.path.exists(DB_PATH):
        return None, None, "Database file not found."

    conn = sqlite3.connect(DB_PATH)

    if not table_exists(conn, "metrics"):
        conn.close()
        return None, None, "The metrics table does not exist yet. Run init_db.py first."

    metrics = pd.read_sql_query(
        """
        SELECT *
        FROM metrics
        ORDER BY timestamp DESC
        LIMIT 1000
        """,
        conn
    )

    if table_exists(conn, "health_scores"):
        health = pd.read_sql_query(
            """
            SELECT *
            FROM health_scores
            ORDER BY current_score ASC
            """,
            conn
        )
    else:
        health = pd.DataFrame()

    conn.close()

    if not metrics.empty:
        metrics["datetime"] = pd.to_datetime(metrics["timestamp"], unit="s")
        metrics["network_total_mb"] = (
            metrics["nic_bytes_sent"].fillna(0) + metrics["nic_bytes_recv"].fillna(0)
        ) / 1_000_000

    if not health.empty:
        health["last_updated_time"] = pd.to_datetime(health["last_updated"], unit="s")

    return metrics, health, None


metrics_df, health_df, error = load_data()

if error:
    st.error(error)
    st.info("Try running: python3 /workspace/dashboard/init_db.py")
    st.stop()

if metrics_df is None or metrics_df.empty:
    st.warning("No metrics found yet.")
    st.info(
        "Generate fake data with: "
        "`python3 /workspace/dashboard/fake_metrics.py --slow node1 --epochs 10 --interval 0.5`"
    )
    st.stop()


# -----------------------------
# Sidebar
# -----------------------------
st.sidebar.header("Dashboard Controls")

available_nodes = sorted(metrics_df["node_id"].dropna().unique())
selected_nodes = st.sidebar.multiselect(
    "Select nodes",
    available_nodes,
    default=available_nodes
)

refresh = st.sidebar.button("Refresh Data")
if refresh:
    st.cache_data.clear()
    st.rerun()

filtered_metrics = metrics_df[metrics_df["node_id"].isin(selected_nodes)]


# -----------------------------
# Section 1: Cluster Status
# -----------------------------
st.header("1. Live Cluster Status")

if health_df is not None and not health_df.empty:
    cols = st.columns(len(health_df))

    for col, row in zip(cols, health_df.itertuples()):
        score = float(row.current_score)

        if score >= 0.75:
            status = "Healthy"
        elif score >= 0.45:
            status = "Watch"
        else:
            status = "Straggler Risk"

        with col:
            st.metric(
                label=row.node_id,
                value=f"{score:.3f}",
                delta=status
            )
            st.progress(score)

    weakest_node = health_df.iloc[0]
    strongest_node = health_df.iloc[-1]

    st.subheader("Cluster Summary")

    c1, c2, c3 = st.columns(3)

    with c1:
        st.metric("Most Likely Straggler", weakest_node["node_id"])

    with c2:
        st.metric("Lowest Health Score", f"{weakest_node['current_score']:.3f}")

    with c3:
        st.metric("Healthiest Node", strongest_node["node_id"])

else:
    st.warning("No health scores found yet.")
    st.info("Run `python3 /workspace/dashboard/health_scores.py` after generating metrics.")


# -----------------------------
# Section 2: Recent Raw Metrics
# -----------------------------
st.header("2. Recent Node Metrics")

latest_by_node = (
    filtered_metrics.sort_values("timestamp")
    .groupby("node_id")
    .tail(1)
    .sort_values("node_id")
)

display_latest = latest_by_node[
    [
        "node_id",
        "job_id",
        "epoch",
        "all_reduce_ms",
        "rtt_ms",
        "network_total_mb",
        "datetime"
    ]
].copy()

display_latest = display_latest.rename(
    columns={
        "node_id": "Node",
        "job_id": "Job ID",
        "epoch": "Epoch",
        "all_reduce_ms": "All-Reduce Time (ms)",
        "rtt_ms": "RTT (ms)",
        "network_total_mb": "Network Total (MB)",
        "datetime": "Timestamp"
    }
)

st.dataframe(display_latest, use_container_width=True)


# -----------------------------
# Section 3: Recent Job Performance
# -----------------------------
st.header("3. Recent Job Performance")

job_summary = (
    filtered_metrics
    .groupby(["job_id", "node_id"])
    .agg(
        avg_all_reduce_ms=("all_reduce_ms", "mean"),
        avg_rtt_ms=("rtt_ms", "mean"),
        avg_network_total_mb=("network_total_mb", "mean"),
        epochs_reported=("epoch", "count"),
        last_seen=("datetime", "max")
    )
    .reset_index()
    .sort_values("last_seen", ascending=False)
)

st.dataframe(job_summary, use_container_width=True)


# -----------------------------
# Section 4: Time Series Charts
# -----------------------------
st.header("4. Time Series Charts")

chart_data = filtered_metrics.sort_values("datetime")

tab1, tab2, tab3 = st.tabs(
    [
        "All-Reduce Time",
        "RTT Latency",
        "Network Throughput"
    ]
)

with tab1:
    fig = px.line(
        chart_data,
        x="datetime",
        y="all_reduce_ms",
        color="node_id",
        markers=True,
        title="All-Reduce Time Over Time"
    )
    st.plotly_chart(fig, use_container_width=True)

with tab2:
    fig = px.line(
        chart_data,
        x="datetime",
        y="rtt_ms",
        color="node_id",
        markers=True,
        title="Round Trip Time Over Time"
    )
    st.plotly_chart(fig, use_container_width=True)

with tab3:
    fig = px.line(
        chart_data,
        x="datetime",
        y="network_total_mb",
        color="node_id",
        markers=True,
        title="Network Bytes Sent + Received Over Time"
    )
    st.plotly_chart(fig, use_container_width=True)


# -----------------------------
# Section 5: Raw Data
# -----------------------------
with st.expander("Show Raw Metrics Data"):
    st.dataframe(filtered_metrics, use_container_width=True)

with st.expander("Show Health Scores Table"):
    if health_df is not None and not health_df.empty:
        st.dataframe(health_df, use_container_width=True)
    else:
        st.write("No health score data available yet.")
