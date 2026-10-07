import unittest
from datetime import datetime, timedelta, timezone

from eligibility import FRESH, STALE, UNKNOWN, evaluate_job, freshness_for, location_match


REFERENCE = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


class TestFreshness(unittest.TestCase):
    def test_relative_window(self):
        self.assertEqual(freshness_for("1 hour ago", reference_time=REFERENCE)[0], FRESH)
        self.assertEqual(freshness_for("23 hours ago", reference_time=REFERENCE)[0], FRESH)
        self.assertEqual(freshness_for("24 hours ago", reference_time=REFERENCE)[0], STALE)
        self.assertEqual(freshness_for("1 day ago", reference_time=REFERENCE)[0], UNKNOWN)
        self.assertEqual(freshness_for(None, reference_time=REFERENCE)[0], UNKNOWN)

    def test_exact_timezone_aware_timestamp(self):
        self.assertEqual(
            freshness_for(
                reference_time=REFERENCE,
                posted_at=REFERENCE - timedelta(hours=23),
            )[0],
            FRESH,
        )
        self.assertEqual(
            freshness_for(
                reference_time=REFERENCE,
                posted_at=datetime(2026, 10, 7, 1),
            )[0],
            UNKNOWN,
        )


class TestPreferences(unittest.TestCase):
    def test_location_aliases_and_non_preferred(self):
        preferred = ["Bangalore", "Chennai", "Remote"]
        self.assertTrue(location_match("Bengaluru", preferred))
        self.assertTrue(location_match("Chennai", preferred))
        self.assertTrue(location_match("WFH", preferred))
        self.assertFalse(location_match("Pune", preferred))
        self.assertIsNone(location_match("Pune", []))

    def test_experience_and_score_rules(self):
        base = {
            "posted_label": "5 hours ago",
            "location": "Bangalore",
            "experience_min": 3,
            "experience_max": 5,
            "match_score": 80,
        }
        self.assertTrue(evaluate_job(base, reference_time=REFERENCE, preferred_locations=["Bangalore"],
                                     applicant_experience=4, resume_ready=True).eligible)
        self.assertFalse(evaluate_job(dict(base, match_score=70), reference_time=REFERENCE,
                                      preferred_locations=["Bangalore"], applicant_experience=4,
                                      resume_ready=True).eligible)
        self.assertFalse(evaluate_job(dict(base, experience_min=6), reference_time=REFERENCE,
                                      preferred_locations=["Bangalore"], applicant_experience=4,
                                      resume_ready=True).eligible)
        self.assertFalse(evaluate_job(dict(base, application_state="recovery_required"),
                                      reference_time=REFERENCE, preferred_locations=["Bangalore"],
                                      applicant_experience=4, resume_ready=True).eligible)


if __name__ == "__main__":
    unittest.main()
