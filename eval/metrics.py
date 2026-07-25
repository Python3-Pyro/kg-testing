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

    Text values fall back to substring containment (not just exact multiset
    membership) before being counted missing: an agent enriching its output
    — e.g. returning "Nicole Simpson (SP01934)" instead of the reference's
    bare "Nicole Simpson" — has still communicated the right information, and
    exact-match-only scoring would wrongly fail it. Numbers stay exact-match
    (post-rounding) only; substring logic on numbers would produce nonsense
    matches (e.g. "1" inside "1630761.93").
    """
    expected_vals = flatten_values(expected_rows)
    actual_vals = flatten_values(actual_rows)
    actual_counts = Counter(actual_vals)
    actual_strings = [v for v in actual_vals if isinstance(v, str)]

    missing = Counter()
    found = 0
    for val in expected_vals:
        if actual_counts[val] > 0:
            actual_counts[val] -= 1
            found += 1
        elif isinstance(val, str) and val and any(val in s for s in actual_strings):
            found += 1
        else:
            missing[val] += 1

    total = len(expected_vals)
    recall = (found / total) if total else 1.0

    return recall == 1.0, recall, {str(k): v for k, v in missing.items()}
