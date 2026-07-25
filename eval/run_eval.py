"""Runs the ground-truth question set against the SQL agent and scores it.

CLI usage: python eval/run_eval.py
Writes a timestamped results file to eval/results/ and prints a summary.

`run_eval()` is also imported directly by the Streamlit Evaluation tab, which
passes in its already-connected/cached SQLAgent instead of opening a new one.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.sql_agent import SQLAgent
from eval.metrics import compare_results

ROOT = Path(__file__).resolve().parent.parent
GROUND_TRUTH_FILE = ROOT / "eval" / "ground_truth.json"
RESULTS_DIR = ROOT / "eval" / "results"


def run_eval(agent: SQLAgent | None = None, progress_callback=None):
    """Scores the agent against eval/ground_truth.json.

    progress_callback(index, total, record), if given, is called after each
    question completes so callers (e.g. Streamlit) can show live progress.

    Returns (summary: dict, records: list[dict], out_file: Path).
    """
    ground_truth = json.loads(GROUND_TRUTH_FILE.read_text())
    owns_agent = agent is None
    if owns_agent:
        agent = SQLAgent()

    records = []
    try:
        for i, item in enumerate(ground_truth, start=1):
            ref_result = agent.run_sql(item["reference_sql"])
            if "error" in ref_result:
                raise RuntimeError(
                    f"Reference SQL failed for {item['id']}: {ref_result['error']}"
                )
            expected_rows = ref_result["rows"]

            start = time.perf_counter()
            try:
                result, _ = agent.ask(item["question"])
                error = None
                actual_rows = result.last_rows
                agent_sql = result.sql_queries
                agent_answer = result.answer
            except Exception as e:
                error = str(e)
                actual_rows = None
                agent_sql = []
                agent_answer = None
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
                "agent_sql": agent_sql,
                "agent_answer": agent_answer,
                "expected_rows": expected_rows,
                "actual_rows": actual_rows,
                "passed": passed,
                "value_recall": recall,
                "missing_values": missing,
                "latency_seconds": round(elapsed, 2),
                "error": error,
            }
            records.append(record)
            if progress_callback:
                progress_callback(i, len(ground_truth), record)
    finally:
        if owns_agent:
            agent.close()

    total = len(records)
    passed_count = sum(1 for r in records if r["passed"])
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "passed": passed_count,
        "accuracy": passed_count / total if total else 0.0,
        "avg_value_recall": sum(r["value_recall"] for r in records) / total if total else 0.0,
        "avg_latency_seconds": sum(r["latency_seconds"] for r in records) / total if total else 0.0,
        "errors": sum(1 for r in records if r["error"]),
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_file = RESULTS_DIR / f"{summary['run_at'].replace(':', '-')}.json"
    out_file.write_text(json.dumps({"summary": summary, "records": records}, indent=2, default=str))

    return summary, records, out_file


def _main():
    def on_progress(i, total, record):
        status = "PASS" if record["passed"] else "FAIL"
        print(f"[{i}/{total}] {record['id']} {status} "
              f"(recall={record['value_recall']:.2f}, {record['latency_seconds']:.1f}s) "
              f"- {record['question']}")

    summary, records, out_file = run_eval(progress_callback=on_progress)

    print("\n=== Summary ===")
    print(f"Accuracy:        {summary['accuracy']:.0%} ({summary['passed']}/{summary['total']})")
    print(f"Avg value recall: {summary['avg_value_recall']:.2f}")
    print(f"Avg latency:      {summary['avg_latency_seconds']:.1f}s")
    print(f"Errors:           {summary['errors']}")
    print(f"Results written to {out_file}")


if __name__ == "__main__":
    _main()
