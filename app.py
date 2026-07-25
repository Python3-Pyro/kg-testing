import json

import streamlit as st

from agent.sql_agent import SQLAgent
from agent.graph_agent import GraphAgent
from eval.run_eval import run_eval, RESULTS_DIR

st.set_page_config(page_title="Retail SQL/Graph Agent", page_icon="🛒", layout="wide")
st.title("🛒 Retail Data Q&A")

BACKENDS = {"sql": "SQL (Postgres)", "graph": "Graph (Neo4j)"}


@st.cache_resource
def get_sql_agent():
    return SQLAgent()


@st.cache_resource
def get_graph_agent():
    return GraphAgent()


def get_agent(backend_label):
    return get_sql_agent() if backend_label == "sql" else get_graph_agent()


def _latest_eval_file(backend_label):
    if not RESULTS_DIR.exists():
        return None
    files = sorted(RESULTS_DIR.glob(f"*__{backend_label}.json"), reverse=True)
    return files[0] if files else None


chat_tab, eval_tab = st.tabs(["💬 Chat", "📊 Evaluation"])

with chat_tab:
    backend_choice = st.radio("Backend", options=list(BACKENDS), format_func=lambda k: BACKENDS[k],
                               horizontal=True, key="chat_backend")

    try:
        agent = get_agent(backend_choice)
    except Exception as e:
        hint = (
            "Make sure `docker compose up -d` is running and the data has been loaded "
            "(`python db/load_data.py`)."
            if backend_choice == "sql" else
            "Make sure `docker compose up -d` is running and the graph has been loaded "
            "(`python -m graph.load_graph`)."
        )
        st.error(f"Couldn't connect to the {BACKENDS[backend_choice]} backend. {hint}\n\nDetails: {e}")
        agent = None

    if agent is not None:
        st.caption("Ask questions about sales, customers, products, stores, and campaigns in plain English.")

        if "chats" not in st.session_state:
            st.session_state.chats = {
                b: {"display_history": [], "api_history": []} for b in BACKENDS
            }
        chat_state = st.session_state.chats[backend_choice]
        query_label = "SQL" if backend_choice == "sql" else "Cypher"

        for turn in chat_state["display_history"]:
            with st.chat_message("user"):
                st.write(turn["question"])
            with st.chat_message("assistant"):
                st.write(turn["answer"])
                if turn["queries"]:
                    with st.expander(f"{query_label} queries used"):
                        for q in turn["queries"]:
                            st.code(q, language="sql" if backend_choice == "sql" else "cypher")
                if turn["rows"]:
                    st.dataframe(turn["rows"], use_container_width=True)
                    if turn["truncated"]:
                        st.caption(f"Results truncated to first {len(turn['rows'])} rows.")

        question = st.chat_input("e.g. Which product category had the highest total sales in Q2?")

        if question:
            with st.chat_message("user"):
                st.write(question)

            with st.chat_message("assistant"):
                with st.spinner("Thinking..."):
                    result, updated_history = agent.ask(question, history=chat_state["api_history"])
                    chat_state["api_history"] = updated_history

                st.write(result.answer)
                if result.queries:
                    with st.expander(f"{query_label} queries used"):
                        for q in result.queries:
                            st.code(q, language="sql" if backend_choice == "sql" else "cypher")
                if result.last_rows:
                    st.dataframe(result.last_rows, use_container_width=True)
                    if result.last_truncated:
                        st.caption(f"Results truncated to first {len(result.last_rows)} rows.")

            chat_state["display_history"].append(
                {
                    "question": question,
                    "answer": result.answer,
                    "queries": result.queries,
                    "columns": result.last_columns,
                    "rows": result.last_rows,
                    "truncated": result.last_truncated,
                }
            )

with eval_tab:
    st.caption(
        "Scores each backend against the same fixed set of ground-truth questions "
        "(`eval/ground_truth.json`) by comparing its results to a reference SQL query "
        "run against Postgres — the reference truth is always Postgres, even when "
        "scoring the graph backend."
    )

    col_sql, col_graph = st.columns(2)

    def render_backend_eval(container, backend_label):
        with container:
            st.subheader(BACKENDS[backend_label])
            run_clicked = st.button(f"▶ Run {BACKENDS[backend_label]} evaluation", key=f"run_{backend_label}")

            if run_clicked:
                try:
                    oracle = get_sql_agent()
                    agent_under_test = get_agent(backend_label)
                except Exception as e:
                    st.error(f"Couldn't connect: {e}")
                    return

                progress_bar = st.progress(0.0)
                status = st.empty()

                def on_progress(i, total, record):
                    progress_bar.progress(i / total)
                    mark = "✅" if record["passed"] else "❌"
                    status.write(f"{mark} [{i}/{total}] {record['question']}")

                with st.spinner("Running evaluation..."):
                    summary, records, out_file = run_eval(
                        oracle=oracle, agent_under_test=agent_under_test,
                        backend_label=backend_label, progress_callback=on_progress,
                    )
                progress_bar.empty()
                status.empty()
                st.success(f"Done. Results saved to `{out_file.name}`.")

            latest_file = _latest_eval_file(backend_label)
            if latest_file is None:
                st.info("No evaluation runs yet.")
                return None

            data = json.loads(latest_file.read_text())
            summary, records = data["summary"], data["records"]
            st.caption(f"Showing results from `{latest_file.name}`")
            st.metric("Accuracy", f"{summary['accuracy']:.0%}", f"{summary['passed']}/{summary['total']} passed")
            m1, m2, m3 = st.columns(3)
            m1.metric("Avg value recall", f"{summary['avg_value_recall']:.2f}")
            m2.metric("Avg latency", f"{summary['avg_latency_seconds']:.1f}s")
            m3.metric("Errors", summary["errors"])
            return records

    records_sql = render_backend_eval(col_sql, "sql")
    records_graph = render_backend_eval(col_graph, "graph")

    st.divider()
    st.subheader("Inspect a question")

    available = [b for b, r in [("sql", records_sql), ("graph", records_graph)] if r]
    if not available:
        st.info("Run an evaluation above to inspect individual questions.")
    else:
        inspect_backend = st.radio("Backend to inspect", options=available, format_func=lambda k: BACKENDS[k],
                                    horizontal=True, key="inspect_backend")
        records = records_sql if inspect_backend == "sql" else records_graph
        query_label = "SQL" if inspect_backend == "sql" else "Cypher"

        table_rows = [
            {
                "id": r["id"],
                "question": r["question"],
                "passed": "✅" if r["passed"] else "❌",
                "value_recall": round(r["value_recall"], 2),
                "latency_s": r["latency_seconds"],
                "tags": ", ".join(r["tags"]),
            }
            for r in records
        ]
        st.dataframe(table_rows, use_container_width=True, hide_index=True)

        selected_id = st.selectbox("Question", options=[r["id"] for r in records],
                                    format_func=lambda i: next(r["question"] for r in records if r["id"] == i),
                                    key=f"select_{inspect_backend}")
        rec = next(r for r in records if r["id"] == selected_id)

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Reference SQL**")
            st.code(rec["reference_sql"], language="sql")
            st.markdown("**Expected rows**")
            st.dataframe(rec["expected_rows"], use_container_width=True)
        with col_b:
            st.markdown(f"**Agent {query_label} queries**")
            for q in rec["agent_queries"]:
                st.code(q, language="sql" if inspect_backend == "sql" else "cypher")
            st.markdown("**Actual rows**")
            st.dataframe(rec["actual_rows"], use_container_width=True)

        if rec["error"]:
            st.error(f"Agent error: {rec['error']}")
        elif not rec["passed"]:
            st.warning(f"Missing expected values: {rec['missing_values']}")

        st.markdown("**Agent's natural-language answer**")
        st.write(rec["agent_answer"])
