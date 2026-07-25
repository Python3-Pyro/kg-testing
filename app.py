import json
from pathlib import Path

import streamlit as st

from agent.sql_agent import SQLAgent
from eval.run_eval import run_eval, RESULTS_DIR

st.set_page_config(page_title="Retail SQL Agent", page_icon="🛒", layout="wide")
st.title("🛒 Retail Data Q&A")


@st.cache_resource
def get_agent():
    return SQLAgent()


try:
    agent = get_agent()
except Exception as e:
    st.error(
        "Couldn't connect to Postgres. Make sure `docker compose up -d` is running "
        f"and the data has been loaded (`python db/load_data.py`).\n\nDetails: {e}"
    )
    st.stop()

chat_tab, eval_tab = st.tabs(["💬 Chat", "📊 Evaluation"])


def _latest_eval_file():
    if not RESULTS_DIR.exists():
        return None
    files = sorted(RESULTS_DIR.glob("*.json"), reverse=True)
    return files[0] if files else None


with chat_tab:
    st.caption("Ask questions about sales, customers, products, stores, and campaigns in plain English.")

    if "display_history" not in st.session_state:
        st.session_state.display_history = []  # [{question, answer, sql_queries, columns, rows, truncated}]
    if "api_history" not in st.session_state:
        st.session_state.api_history = []  # raw Anthropic message history for multi-turn context

    for turn in st.session_state.display_history:
        with st.chat_message("user"):
            st.write(turn["question"])
        with st.chat_message("assistant"):
            st.write(turn["answer"])
            if turn["sql_queries"]:
                with st.expander("SQL queries used"):
                    for q in turn["sql_queries"]:
                        st.code(q, language="sql")
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
                result, updated_history = agent.ask(question, history=st.session_state.api_history)
                st.session_state.api_history = updated_history

            st.write(result.answer)
            if result.sql_queries:
                with st.expander("SQL queries used"):
                    for q in result.sql_queries:
                        st.code(q, language="sql")
            if result.last_rows:
                st.dataframe(result.last_rows, use_container_width=True)
                if result.last_truncated:
                    st.caption(f"Results truncated to first {len(result.last_rows)} rows.")

        st.session_state.display_history.append(
            {
                "question": question,
                "answer": result.answer,
                "sql_queries": result.sql_queries,
                "columns": result.last_columns,
                "rows": result.last_rows,
                "truncated": result.last_truncated,
            }
        )

with eval_tab:
    st.caption(
        "Scores the agent against a fixed set of ground-truth questions "
        "(`eval/ground_truth.json`) by comparing its SQL results to a reference query."
    )

    if st.button("▶ Run evaluation now", type="primary"):
        progress_bar = st.progress(0.0)
        status = st.empty()

        def on_progress(i, total, record):
            progress_bar.progress(i / total)
            mark = "✅" if record["passed"] else "❌"
            status.write(f"{mark} [{i}/{total}] {record['question']}")

        with st.spinner("Running evaluation..."):
            summary, records, out_file = run_eval(agent=agent, progress_callback=on_progress)
        progress_bar.empty()
        status.empty()
        st.success(f"Done. Results saved to `{out_file.name}`.")

    latest_file = _latest_eval_file()
    if latest_file is None:
        st.info("No evaluation runs yet. Click **Run evaluation now** to score the agent.")
    else:
        data = json.loads(latest_file.read_text())
        summary, records = data["summary"], data["records"]

        st.caption(f"Showing results from `{latest_file.name}`")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Accuracy", f"{summary['accuracy']:.0%}", f"{summary['passed']}/{summary['total']} passed")
        c2.metric("Avg value recall", f"{summary['avg_value_recall']:.2f}")
        c3.metric("Avg latency", f"{summary['avg_latency_seconds']:.1f}s")
        c4.metric("Errors", summary["errors"])

        st.divider()

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

        st.subheader("Inspect a question")
        selected_id = st.selectbox("Question", options=[r["id"] for r in records],
                                    format_func=lambda i: next(r["question"] for r in records if r["id"] == i))
        rec = next(r for r in records if r["id"] == selected_id)

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Reference SQL**")
            st.code(rec["reference_sql"], language="sql")
            st.markdown("**Expected rows**")
            st.dataframe(rec["expected_rows"], use_container_width=True)
        with col_b:
            st.markdown("**Agent SQL**")
            for q in rec["agent_sql"]:
                st.code(q, language="sql")
            st.markdown("**Actual rows**")
            st.dataframe(rec["actual_rows"], use_container_width=True)

        if rec["error"]:
            st.error(f"Agent error: {rec['error']}")
        elif not rec["passed"]:
            st.warning(f"Missing expected values: {rec['missing_values']}")

        st.markdown("**Agent's natural-language answer**")
        st.write(rec["agent_answer"])
