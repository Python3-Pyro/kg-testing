"""Scoring logic shared by eval/run_eval.py and the Streamlit Evaluation tab.

The agent is free to name/order/alias columns however the model likes, so we
can't compare result sets structurally (row-for-row, column-for-column)
against a reference query. Instead we flatten both result sets down to a
multiset of normalized scalar values and check that every value the
reference query produced also appears in the agent's result. This is a
looser check than exact matching, but it's robust to the kind of harmless
differences (column names, extra descriptive columns, row order) a SQL
agent produces, while still catching wrong numbers, missing groups, or
wrong filters.
"""
from collections import Counter
from datetime import date, datetime
from decimal import Decimal


def _normalize_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return round(float(v), 2)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v).strip().lower()


def flatten_values(rows):
    values = []
    for row in rows or []:
        cells = row.values() if isinstance(row, dict) else row
        values.extend(_normalize_value(v) for v in cells)
    return values


def compare_results(expected_rows, actual_rows):
    """Returns (passed, recall, missing_values).

    recall = fraction of expected scalar values found in the actual result.
    passed = recall == 1.0 (every expected value was found).
    missing_values = {normalized_value: count} for anything not found.
    """
    expected_vals = Counter(flatten_values(expected_rows))
    actual_vals = Counter(flatten_values(actual_rows))
    missing = expected_vals - actual_vals

    total = sum(expected_vals.values())
    found = total - sum(missing.values())
    recall = (found / total) if total else 1.0

    return recall == 1.0, recall, {str(k): v for k, v in missing.items()}
