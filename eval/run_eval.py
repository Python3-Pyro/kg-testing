"""Runs the ground-truth question set against a SQL or Graph agent and scores it.

CLI usage:
    python -m eval.run_eval                  (SQL agent, default)
    python -m eval.run_eval --backend graph   (Graph agent)
Writes a timestamped, backend-labeled results file to eval/results/ and
prints a summary.

`run_eval()` is also imported directly by the Streamlit Evaluation tab, which
passes in its already-connected/cached agent(s) instead of opening new ones.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.providers import DEFAULT_MODEL_KEY, MODEL_REGISTRY
from agent.sql_agent import SQLAgent
from eval.metrics import compare_results

ROOT = Path(__file__).resolve().parent.parent
GROUND_TRUTH_FILE = ROOT / "eval" / "ground_truth.json"
RESULTS_DIR = ROOT / "eval" / "results"


def run_eval(oracle: SQLAgent | None = None, agent_under_test=None,
             backend_label: str = "sql", model_key: str = DEFAULT_MODEL_KEY, progress_callback=None):
    """Scores `agent_under_test` against eval/ground_truth.json.

    `oracle` always runs the reference SQL (the scoring ground truth) against
    Postgres, regardless of which backend is under test — a GraphAgent has no
    way to answer the reference query itself, and the oracle never calls an
    LLM, so its own model is irrelevant. If `oracle` is omitted, one is
    created and owned (closed) by this call. `agent_under_test` defaults to
    `oracle` itself, reproducing the original SQL-agent-scores-itself
    behavior; pass a GraphAgent to score the graph backend instead. Callers
    that pass their own agent_under_test are responsible for closing it —
    this function only closes what it created (the default oracle).

    `model_key` is purely descriptive here (recorded in the summary/filename)
    — the actual model is whatever `agent_under_test` was constructed with;
    pass the matching key so results stay attributable to the right model.

    progress_callback(index, total, record), if given, is called after each
    question completes so callers (e.g. Streamlit) can show live progress.

    Returns (summary: dict, records: list[dict], out_file: Path).
    """
    ground_truth = json.loads(GROUND_TRUTH_FILE.read_text())

    owns_oracle = oracle is None
    if owns_oracle:
        oracle = SQLAgent()
    if agent_under_test is None:
        agent_under_test = oracle

    records = []
    try:
        for i, item in enumerate(ground_truth, start=1):
            ref_result = oracle.run_sql(item["reference_sql"])
            if "error" in ref_result:
                raise RuntimeError(
                    f"Reference SQL failed for {item['id']}: {ref_result['error']}"
                )
            expected_rows = ref_result["rows"]

            start = time.perf_counter()
            try:
                result, _ = agent_under_test.ask(item["question"])
                error = None
                actual_rows = result.last_rows
                agent_queries = result.queries
                agent_answer = result.answer
                input_tokens = result.input_tokens
                output_tokens = result.output_tokens
            except Exception as e:
                error = str(e)
                actual_rows = None
                agent_queries = []
                agent_answer = None
                input_tokens = 0
                output_tokens = 0
            elapsed = time.perf_counter() - start

            if error:
                passed, recall, missing = False, 0.0, {}
            else:
                passed, recall, missing = compare_results(expected_rows, actual_rows)

            record = {
                "id": item["id"],
                "question": item["question"],
                "tags": item.get("tags", []),
                "reference_sql": item["reference_sql"],
                "agent_queries": agent_queries,
                "agent_answer": agent_answer,
                "expected_rows": expected_rows,
                "actual_rows": actual_rows,
                "passed": passed,
                "value_recall": recall,
                "missing_values": missing,
                "latency_seconds": round(elapsed, 2),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "error": error,
            }
            records.append(record)
            if progress_callback:
                progress_callback(i, len(ground_truth), record)
    finally:
        if owns_oracle:
            oracle.close()

    total = len(records)
    passed_count = sum(1 for r in records if r["passed"])
    summary = {
        "backend": backend_label,
        "model_key": model_key,
        "model_label": MODEL_REGISTRY.get(model_key, {}).get("label", model_key),
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "passed": passed_count,
        "accuracy": passed_count / total if total else 0.0,
        "avg_value_recall": sum(r["value_recall"] for r in records) / total if total else 0.0,
        "avg_latency_seconds": sum(r["latency_seconds"] for r in records) / total if total else 0.0,
        "avg_input_tokens": sum(r["input_tokens"] for r in records) / total if total else 0.0,
        "avg_output_tokens": sum(r["output_tokens"] for r in records) / total if total else 0.0,
        "avg_total_tokens": sum(r["total_tokens"] for r in records) / total if total else 0.0,
        "total_tokens_all_questions": sum(r["total_tokens"] for r in records),
        "errors": sum(1 for r in records if r["error"]),
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_file = RESULTS_DIR / f"{summary['run_at'].replace(':', '-')}__{backend_label}__{model_key}.json"
    out_file.write_text(json.dumps({"summary": summary, "records": records}, indent=2, default=str))

    return summary, records, out_file


def _main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["sql", "graph"], default="sql")
    parser.add_argument("--model", choices=list(MODEL_REGISTRY), default=DEFAULT_MODEL_KEY)
    args = parser.parse_args()

    def on_progress(i, total, record):
        status = "PASS" if record["passed"] else "FAIL"
        print(f"[{i}/{total}] {record['id']} {status} "
              f"(recall={record['value_recall']:.2f}, {record['latency_seconds']:.1f}s, "
              f"{record['total_tokens']} tokens) - {record['question']}")

    oracle = SQLAgent()
    agent_under_test = None
    try:
        if args.backend == "graph":
            from agent.graph_agent import GraphAgent
            agent_under_test = GraphAgent(model_key=args.model)
        else:
            agent_under_test = SQLAgent(model_key=args.model)

        summary, records, out_file = run_eval(
            oracle=oracle,
            agent_under_test=agent_under_test,
            backend_label=args.backend,
            model_key=args.model,
            progress_callback=on_progress,
        )
    finally:
        oracle.close()
        if agent_under_test is not None and agent_under_test is not oracle:
            agent_under_test.close()

    print("\n=== Summary ===")
    print(f"Backend:          {summary['backend']}")
    print(f"Model:            {summary['model_label']}")
    print(f"Accuracy:         {summary['accuracy']:.0%} ({summary['passed']}/{summary['total']})")
    print(f"Avg value recall: {summary['avg_value_recall']:.2f}")
    print(f"Avg latency:      {summary['avg_latency_seconds']:.1f}s")
    print(f"Avg tokens:       {summary['avg_total_tokens']:.0f} "
          f"(in: {summary['avg_input_tokens']:.0f}, out: {summary['avg_output_tokens']:.0f})")
    print(f"Errors:           {summary['errors']}")
    print(f"Results written to {out_file}")


if __name__ == "__main__":
    _main()
