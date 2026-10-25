"""HU036 (ADR-044 §8-§10): regla `HR_ABOVE_THRESHOLD_V1`, umbral por defecto, resumen del lote
y finalización con `RISK_STOP`, sin infraestructura."""

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from training_management.domain.monitoring.model import (
    DEFAULT_THRESHOLD,
    EvaluationStatus,
    InvalidMonitoringSettings,
    MonitoringSettings,
    ThresholdSource,
    alert_id,
    default_threshold,
    evaluate,
    summarize,
    validate_settings,
    window_start,
)
from training_management.domain.plans.model import Sport
from training_management.domain.sessions.model import (
    SessionEndReason,
    StartData,
    TrainingSession,
    validate_finish,
)
from training_management.domain.sessions.validation import InvalidTrainingSession

NOW = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
SESSION = uuid4()
DEVICE = "a" * 64


def run(start: datetime, seconds: int, value: int) -> list[tuple[datetime, int]]:
    """`seconds` muestras de 1 Hz desde `start`."""
    return [(start + timedelta(seconds=offset), value) for offset in range(seconds)]


def judge(
    samples: list[tuple[datetime, int]],
    new: list[datetime] | None = None,
    threshold: int = 150,
    previous: list[datetime] | None = None,
    now: datetime | None = None,
):  # type: ignore[no-untyped-def]
    return evaluate(
        session_id=SESSION,
        device_id=DEVICE,
        samples=samples,
        new_instants=new if new is not None else [instant for instant, _ in samples],
        threshold=threshold,
        previous_alerts=previous or [],
        now=now or (samples[-1][0] if samples else NOW),
    )


class HeartRateRuleTest(unittest.TestCase):
    """CA2 y CA3."""

    def test_fifteen_consecutive_seconds_above_threshold_is_risk(self) -> None:
        start = NOW - timedelta(seconds=14)
        evaluation = judge(run(start, 15, 151))
        self.assertIs(evaluation.status, EvaluationStatus.RISK)
        self.assertEqual(len(evaluation.alerts), 1)
        alert = evaluation.alerts[0]
        self.assertEqual(
            (alert.rule, alert.severity.value, alert.detected_at, alert.threshold_bpm),
            ("HR_ABOVE_THRESHOLD_V1", "HIGH", NOW, 150),
        )
        self.assertEqual(alert.alert_id, alert_id(SESSION, DEVICE, NOW))

    def test_fourteen_seconds_or_values_equal_to_threshold_are_ok(self) -> None:
        self.assertIs(judge(run(NOW - timedelta(seconds=13), 14, 180)).status, EvaluationStatus.OK)
        equal = judge(run(NOW - timedelta(seconds=20), 21, 150))
        self.assertEqual((equal.status, equal.alerts), (EvaluationStatus.OK, ()))

    def test_a_gap_breaks_the_window(self) -> None:
        """Ventana de 15 s con huecos: sin una muestra por segundo no hay racha."""
        start = NOW - timedelta(seconds=15)
        samples = [item for item in run(start, 16, 170) if item[0] != start + timedelta(seconds=7)]
        evaluation = judge(samples)
        self.assertEqual((evaluation.status, evaluation.alerts), (EvaluationStatus.OK, ()))

    def test_a_value_below_threshold_restarts_the_window(self) -> None:
        start = NOW - timedelta(seconds=20)
        samples = run(start, 21, 170)
        samples[5] = (samples[5][0], 120)
        self.assertIs(judge(samples[:-1]).status, EvaluationStatus.OK)
        evaluation = judge(samples)
        # La racha vuelve a empezar en la muestra 6 y llega a 15 s en la última.
        self.assertIs(evaluation.status, EvaluationStatus.RISK)
        self.assertEqual([alert.detected_at for alert in evaluation.alerts], [NOW])

    def test_alert_repeats_at_most_every_sixty_seconds(self) -> None:
        start = NOW - timedelta(seconds=89)
        samples = run(start, 90, 175)
        evaluation = judge(samples)
        first = start + timedelta(seconds=14)
        self.assertEqual(
            [alert.detected_at for alert in evaluation.alerts],
            [first, first + timedelta(seconds=60)],
        )

    def test_recent_alert_suppresses_a_new_one_but_status_stays_risk(self) -> None:
        samples = run(NOW - timedelta(seconds=29), 30, 175)
        last_batch = [instant for instant, _ in samples[-5:]]
        evaluation = judge(samples, new=last_batch, previous=[NOW - timedelta(seconds=59)])
        self.assertEqual((evaluation.status, evaluation.alerts), (EvaluationStatus.RISK, ()))
        later = judge(samples, new=last_batch, previous=[NOW - timedelta(seconds=60)])
        self.assertEqual([alert.detected_at for alert in later.alerts], [NOW])

    def test_old_samples_already_evaluated_do_not_alert_again(self) -> None:
        samples = run(NOW - timedelta(seconds=29), 30, 175)
        evaluation = judge(samples, new=[])
        self.assertEqual((evaluation.status, evaluation.alerts), (EvaluationStatus.RISK, ()))

    def test_no_data_after_thirty_seconds(self) -> None:
        """CA3: la última muestra con más de 30 s es pérdida de monitoreo, no un valor actual."""
        samples = run(NOW - timedelta(seconds=60), 20, 190)
        stale = judge(samples, now=NOW)
        self.assertEqual((stale.status, stale.alerts), (EvaluationStatus.NO_DATA, ()))
        latest = samples[-1][0]
        self.assertIs(
            judge(samples, now=latest + timedelta(seconds=30)).status, EvaluationStatus.RISK
        )
        self.assertIs(
            judge(samples, now=latest + timedelta(seconds=31)).status, EvaluationStatus.NO_DATA
        )
        self.assertIs(judge([]).status, EvaluationStatus.NO_DATA)

    def test_out_of_order_samples_complete_the_window(self) -> None:
        samples = run(NOW - timedelta(seconds=14), 15, 160)
        middle = samples[7][0]
        evaluation = judge(samples, new=[middle])
        self.assertEqual([alert.detected_at for alert in evaluation.alerts], [])
        last = judge(samples, new=[samples[-1][0]])
        self.assertEqual([alert.detected_at for alert in last.alerts], [NOW])

    def test_window_starts_thirty_seconds_before_the_oldest_new_sample(self) -> None:
        self.assertEqual(
            window_start([NOW, NOW - timedelta(seconds=4)], NOW),
            NOW - timedelta(seconds=34),
        )
        self.assertEqual(window_start([], NOW), NOW - timedelta(seconds=30))

    def test_evaluation_is_deterministic(self) -> None:
        samples = run(NOW - timedelta(seconds=40), 41, 180)
        self.assertEqual(judge(samples), judge(list(reversed(samples))))


class DefaultThresholdTest(unittest.TestCase):
    def test_age_based_default_rounds_half_up(self) -> None:
        today = date(2026, 10, 24)
        # 25 años: 0.9 x 195 = 175.5 → 176.
        threshold = default_threshold(date(2001, 10, 24), today)
        self.assertEqual((threshold.value, threshold.source), (176, ThresholdSource.AGE_DEFAULT))
        # Un día antes del cumpleaños sigue con 24 años: 0.9 x 196 = 176.4 → 176.
        self.assertEqual(default_threshold(date(2001, 10, 25), today).value, 176)
        # 40 años: 0.9 x 180 = 162.
        self.assertEqual(default_threshold(date(1986, 1, 1), today).value, 162)

    def test_without_birth_date_is_171(self) -> None:
        threshold = default_threshold(None, date(2026, 10, 24))
        self.assertEqual(
            (threshold.value, threshold.source, threshold.settings),
            (DEFAULT_THRESHOLD, ThresholdSource.FIXED_DEFAULT, None),
        )
        self.assertEqual(DEFAULT_THRESHOLD, 171)

    def test_default_is_clamped_to_the_configurable_range(self) -> None:
        today = date(2026, 10, 24)
        self.assertEqual(default_threshold(date(1900, 1, 1), today).value, 100)
        self.assertEqual(default_threshold(today, today).value, 198)


class MonitoringSettingsTest(unittest.TestCase):
    def test_valid_threshold(self) -> None:
        self.assertEqual(validate_settings({"max_heart_rate_alert_bpm": 100}), 100)
        self.assertEqual(validate_settings({"max_heart_rate_alert_bpm": 220}), 220)

    def test_invalid_bodies_report_every_rule(self) -> None:
        cases = [
            (None, {(None, "required")}),
            ([], {(None, "unknown_value")}),
            ({}, {("max_heart_rate_alert_bpm", "required")}),
            ({"max_heart_rate_alert_bpm": 99}, {("max_heart_rate_alert_bpm", "out_of_range")}),
            ({"max_heart_rate_alert_bpm": 221}, {("max_heart_rate_alert_bpm", "out_of_range")}),
            ({"max_heart_rate_alert_bpm": 150.5}, {("max_heart_rate_alert_bpm", "unknown_value")}),
            ({"max_heart_rate_alert_bpm": True}, {("max_heart_rate_alert_bpm", "unknown_value")}),
            (
                {"max_heart_rate_alert_bpm": 150, "athlete_id": "x"},
                {("athlete_id", "field_not_allowed")},
            ),
        ]
        for body, expected in cases:
            with self.subTest(body=body), self.assertRaises(InvalidMonitoringSettings) as caught:
                validate_settings(body)
            self.assertEqual(set(caught.exception.violations), expected)

    def test_same_threshold_is_unchanged_and_another_bumps_the_version(self) -> None:
        settings = MonitoringSettings.create(uuid4(), 160, NOW)
        self.assertIs(settings.change(160, NOW + timedelta(hours=1)), settings)
        changed = settings.change(150, NOW + timedelta(hours=1))
        self.assertEqual(
            (changed.max_heart_rate_alert_bpm, changed.version, changed.updated_at),
            (150, 2, NOW + timedelta(hours=1)),
        )


class BatchSummaryTest(unittest.TestCase):
    def test_summary_of_out_of_order_samples(self) -> None:
        summary = summarize(
            SESSION,
            DEVICE,
            [(NOW, 130), (NOW - timedelta(seconds=2), 140), (NOW - timedelta(seconds=1), 121)],
            3,
        )
        self.assertEqual(
            (
                summary.window_from,
                summary.window_to,
                summary.samples_accepted,
                summary.last_value,
                summary.max_value,
                summary.avg_value,
                summary.version,
            ),
            (NOW - timedelta(seconds=2), NOW, 3, 130, 140, Decimal("130.3"), 3),
        )
        self.assertEqual(
            summarize(SESSION, DEVICE, [(NOW, 121), (NOW, 122)], 1).avg_value, Decimal("121.5")
        )

    def test_summary_needs_samples(self) -> None:
        with self.assertRaises(ValueError):
            summarize(SESSION, DEVICE, [], 1)


class RiskStopTest(unittest.TestCase):
    """CA4: finalización única con `RISK_STOP`."""

    def session(self) -> TrainingSession:
        return TrainingSession.start(
            athlete_id=uuid4(),
            data=StartData(uuid4(), None, Sport.RUNNING, NOW - timedelta(minutes=30)),
            now=NOW - timedelta(minutes=30),
        )

    def test_finish_keeps_the_reason(self) -> None:
        finished = self.session().finish(ended_at=NOW, now=NOW, reason=SessionEndReason.RISK_STOP)
        self.assertIs(finished.end_reason, SessionEndReason.RISK_STOP)
        self.assertTrue(finished.matches_finish(NOW, SessionEndReason.RISK_STOP))
        self.assertFalse(finished.matches_finish(NOW, None))
        self.assertIsNone(self.session().finish(ended_at=NOW, now=NOW).end_reason)

    def test_reason_is_validated(self) -> None:
        session_id = str(uuid4())
        data = validate_finish(session_id, "2026-10-24T15:00:00Z", (), NOW, "RISK_STOP")
        self.assertIs(data.reason, SessionEndReason.RISK_STOP)
        self.assertIsNone(validate_finish(session_id, "2026-10-24T15:00:00Z", (), NOW).reason)
        with self.assertRaises(InvalidTrainingSession) as caught:
            validate_finish(session_id, "2026-10-24T15:00:00Z", (), NOW, "TIRED")
        self.assertEqual(
            [(item.field, item.issue.value) for item in caught.exception.violations],
            [("reason", "unknown_value")],
        )


if __name__ == "__main__":
    unittest.main()
