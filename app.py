import json

import streamlit as st

from agent.sql_agent import SQLAgent
from agent.graph_agent import GraphAgent
from agent.providers import DEFAULT_MODEL_KEY, MODEL_REGISTRY
from eval.run_eval import run_eval, RESULTS_DIR

st.set_page_config(page_title="Retail SQL/Graph Agent", page_icon="🛒", layout="wide")
st.title("🛒 Retail Data Q&A")

BACKENDS = {"sql": "SQL (Postgres)", "graph": "Graph (Neo4j)"}


def model_label(model_key):
    return MODEL_REGISTRY.get(model_key, {}).get("label", model_key)


def _api_key_hint(model_key):
    provider = MODEL_REGISTRY.get(model_key, {}).get("provider")
    env_var = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
               "openrouter": "OPENROUTER_API_KEY"}.get(provider, "")
    return f" Check `{env_var}` is set in `.env`." if env_var else ""


@st.cache_resource
def get_sql_agent(model_key):
    return SQLAgent(model_key=model_key)


@st.cache_resource
def get_graph_agent(model_key):
    return GraphAgent(model_key=model_key)


def get_agent(backend_label, model_key):
    return get_sql_agent(model_key) if backend_label == "sql" else get_graph_agent(model_key)


def model_selector(key, default=DEFAULT_MODEL_KEY):
    options = list(MODEL_REGISTRY)
    return st.selectbox("Model", options=options, index=options.index(default),
                         format_func=model_label, key=key)


def _latest_eval_file(backend_label, model_key):
    if not RESULTS_DIR.exists():
        return None
    files = sorted(RESULTS_DIR.glob(f"*__{backend_label}__{model_key}.json"), reverse=True)
    return files[0] if files else None


chat_tab, eval_tab = st.tabs(["💬 Chat", "📊 Evaluation"])

with chat_tab:
    col_backend, col_model = st.columns([1, 1])
    with col_backend:
        backend_choice = st.radio("Backend", options=list(BACKENDS), format_func=lambda k: BACKENDS[k],
                                   horizontal=True, key="chat_backend")
    with col_model:
        model_choice = model_selector("chat_model")

    try:
        agent = get_agent(backend_choice, model_choice)
    except Exception as e:
        hint = (
            "Make sure `docker compose up -d` is running and the data has been loaded "
            "(`python db/load_data.py`)."
            if backend_choice == "sql" else
            "Make sure `docker compose up -d` is running and the graph has been loaded "
            "(`python -m graph.load_graph`)."
        )
        st.error(f"Couldn't connect to {BACKENDS[backend_choice]} with {model_label(model_choice)}. "
                  f"{hint}{_api_key_hint(model_choice)}\n\nDetails: {e}")
        agent = None

    if agent is not None:
        st.caption("Ask questions about sales, customers, products, stores, and campaigns in plain English.")

        if "chats" not in st.session_state:
            st.session_state.chats = {}
        chat_key = f"{backend_choice}__{model_choice}"
        if chat_key not in st.session_state.chats:
            st.session_state.chats[chat_key] = {"display_history": [], "api_history": []}
        chat_state = st.session_state.chats[chat_key]
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
                with st.spinner(f"Thinking ({model_label(model_choice)})..."):
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
                st.caption(f"{result.total_tokens:,} tokens (in: {result.input_tokens:,}, out: {result.output_tokens:,})")

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
        "Scores a backend+model combination against the same fixed set of ground-truth "
        "questions (`eval/ground_truth.json`) by comparing its results to a reference SQL "
        "query run against Postgres — the reference truth is always Postgres, regardless "
        "of which backend or model is under test."
    )

    col_sql, col_graph = st.columns(2)

    def render_backend_eval(container, backend_label):
        with container:
            st.subheader(BACKENDS[backend_label])
            model_key = model_selector(f"eval_model_{backend_label}")
            run_clicked = st.button("▶ Run evaluation", key=f"run_{backend_label}")

            if run_clicked:
                try:
                    oracle = get_sql_agent(DEFAULT_MODEL_KEY)  # oracle never calls an LLM; kept on a fixed cheap model
                    agent_under_test = get_agent(backend_label, model_key)
                except Exception as e:
                    st.error(f"Couldn't connect ({model_label(model_key)}): {e}{_api_key_hint(model_key)}")
                    return

                progress_bar = st.progress(0.0)
                status = st.empty()

                def on_progress(i, total, record):
                    progress_bar.progress(i / total)
                    mark = "✅" if record["passed"] else "❌"
                    status.write(f"{mark} [{i}/{total}] {record['question']}")

                with st.spinner(f"Running evaluation ({model_label(model_key)})..."):
                    summary, records, out_file = run_eval(
                        oracle=oracle, agent_under_test=agent_under_test,
                        backend_label=backend_label, model_key=model_key, progress_callback=on_progress,
                    )
                progress_bar.empty()
                status.empty()
                st.success(f"Done. Results saved to `{out_file.name}`.")

            latest_file = _latest_eval_file(backend_label, model_key)
            if latest_file is None:
                st.info(f"No evaluation runs yet for {model_label(model_key)}.")
                return None

            data = json.loads(latest_file.read_text())
            summary, records = data["summary"], data["records"]
            st.caption(f"Showing results from `{latest_file.name}`")
            st.metric("Accuracy", f"{summary['accuracy']:.0%}", f"{summary['passed']}/{summary['total']} passed")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Avg value recall", f"{summary['avg_value_recall']:.2f}")
            m2.metric("Avg latency", f"{summary['avg_latency_seconds']:.1f}s")
            m3.metric("Avg tokens", f"{summary.get('avg_total_tokens', 0):.0f}")
            m4.metric("Errors", summary["errors"])
            return summary, records

    sql_result = render_backend_eval(col_sql, "sql")
    graph_result = render_backend_eval(col_graph, "graph")
    summary_sql, records_sql = sql_result if sql_result else (None, None)
    summary_graph, records_graph = graph_result if graph_result else (None, None)

    if summary_sql and summary_graph:
        st.divider()
        st.subheader(f"Tokenomics: {summary_sql['model_label']} (SQL) vs. {summary_graph['model_label']} (Graph)")
        sql_tok = summary_sql.get("avg_total_tokens", 0)
        graph_tok = summary_graph.get("avg_total_tokens", 0)
        delta_pct = ((graph_tok - sql_tok) / sql_tok * 100) if sql_tok else 0.0
        t1, t2, t3 = st.columns(3)
        t1.metric("SQL avg tokens/question", f"{sql_tok:.0f}")
        t2.metric("Graph avg tokens/question", f"{graph_tok:.0f}", f"{delta_pct:+.0f}% vs SQL")
        t3.metric("SQL total / Graph total (all questions)",
                   f"{summary_sql.get('total_tokens_all_questions', 0):,} / "
                   f"{summary_graph.get('total_tokens_all_questions', 0):,}")

    st.divider()
    st.subheader("Inspect a question")

    available = [b for b, r in [("sql", records_sql), ("graph", records_graph)] if r]
    if not available:
        st.info("Run an evaluation above to inspect individual questions.")
    else:
        inspect_backend = st.radio("Backend to inspect", options=available, format_func=lambda k: BACKENDS[k],
                                    horizontal=True, key="inspect_backend")
        records = records_sql if inspect_backend == "sql" else records_graph
        inspect_summary = summary_sql if inspect_backend == "sql" else summary_graph
        query_label = "SQL" if inspect_backend == "sql" else "Cypher"
        st.caption(f"Model: {inspect_summary['model_label']}")

        table_rows = [
            {
                "id": r["id"],
                "question": r["question"],
                "passed": "✅" if r["passed"] else "❌",
                "value_recall": round(r["value_recall"], 2),
                "latency_s": r["latency_seconds"],
                "tokens": r.get("total_tokens", 0),
                "tags": ", ".join(r["tags"]),
            }
            for r in records
        ]
        st.dataframe(table_rows, use_container_width=True, hide_index=True)

        selected_id = st.selectbox("Question", options=[r["id"] for r in records],
                                    format_func=lambda i: next(r["question"] for r in records if r["id"] == i),
                                    key=f"select_{inspect_backend}")
        rec = next(r for r in records if r["id"] == selected_id)
        st.caption(
            f"{rec.get('total_tokens', 0):,} tokens "
            f"(in: {rec.get('input_tokens', 0):,}, out: {rec.get('output_tokens', 0):,}) · "
            f"{rec['latency_seconds']:.1f}s"
        )

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
