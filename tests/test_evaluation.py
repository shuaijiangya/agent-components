import pytest

from agent_components.evaluation import (
    RankedResult,
    RelevanceLabel,
    evaluate_retrieval,
    evaluate_schema,
)


def test_schema_metric_reports_missing_required_field():
    result = evaluate_schema({}, {"type": "object", "required": ["contract_id"]})
    assert not result.passed
    assert result.errors


@pytest.mark.integration
def test_ir_metrics_against_known_relevance_judgements():
    pytest.importorskip("ir_measures")
    scores = evaluate_retrieval(
        [
            RelevanceLabel(query_id="q", item_id="a", relevance=1),
            RelevanceLabel(query_id="q", item_id="b", relevance=0),
        ],
        [
            RankedResult(query_id="q", item_id="a", score=1.0),
            RankedResult(query_id="q", item_id="b", score=0.1),
        ],
        k=1,
    )
    assert scores == {"R@1": 1.0, "nDCG@1": 1.0, "RR@1": 1.0}
