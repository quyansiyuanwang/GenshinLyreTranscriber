from glt_core.tools.quality_regression import ExpectedEvent, match_events


def test_match_events_computes_precision_recall_and_f1() -> None:
    expected = [
        ExpectedEvent(1_000_000, ("A",)),
        ExpectedEvent(2_000_000, ("D",)),
    ]
    actual = [
        ExpectedEvent(1_050_000, ("A",)),
        ExpectedEvent(2_100_000, ("F",)),
        ExpectedEvent(3_000_000, ("Q",)),
    ]
    score = match_events(expected, actual, 120_000)
    assert score.true_positive == 1
    assert score.false_positive == 2
    assert score.false_negative == 1
    assert score.precision == 1 / 3
    assert score.recall == 0.5
    assert round(score.f1, 6) == 0.4


def test_match_events_respects_tolerance() -> None:
    expected = [ExpectedEvent(1_000_000, ("A",))]
    actual = [ExpectedEvent(1_200_001, ("A",))]
    assert match_events(expected, actual, 120_000).true_positive == 0
