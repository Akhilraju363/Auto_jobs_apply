"""Phase 3.1: applicant profile configuration, validation and master-resume evidence."""

import tempfile
import unittest
from pathlib import Path

from application_profile import FACT_FIELDS, PREFERENCE_FIELDS, ApplicationProfile, ResumeEvidence


class TestProfileConfiguration(unittest.TestCase):
    def test_empty_environment_is_unconfigured(self):
        profile = ApplicationProfile.from_environment({})
        for name in FACT_FIELDS + PREFERENCE_FIELDS:
            self.assertIsNone(getattr(profile, name), name)
            value, reason = profile.answer_for(name)
            self.assertIsNone(value)
            self.assertIn("not configured", reason)
        self.assertEqual(profile.configured(), [])

    def test_blank_values_become_none(self):
        profile = ApplicationProfile.from_environment({"APPLICANT_EXPECTED_CTC": "   ", "APPLICANT_NOTICE_PERIOD": ""})
        self.assertIsNone(profile.expected_ctc)
        self.assertIsNone(profile.notice_period)

    def test_configured_values_are_used_as_written(self):
        env = {"APPLICANT_EXPECTED_CTC": " 12 ", "APPLICANT_NOTICE_PERIOD": "30 days", "APPLICANT_CURRENT_LOCATION": "Pune"}
        profile = ApplicationProfile.from_environment(env)
        self.assertEqual(profile.answer_for("expected_ctc"), ("12", ""))
        self.assertEqual(profile.answer_for("notice_period"), ("30 days", ""))
        self.assertEqual(profile.answer_for("current_location"), ("Pune", ""))
        self.assertEqual(sorted(profile.configured()), ["current_location", "expected_ctc", "notice_period"])

    def test_validation_accepts_common_formats(self):
        accepted = {
            "expected_ctc": ["12", "12.5", "12 LPA", "12 lakhs", "12 lacs"],
            "current_ctc": ["8", "8.4 LPA"],
            "total_experience": ["4", "4.5", "4 years", "4 yrs"],
            "notice_period": ["30", "30 days", "Immediate", "15 days", "2 months", "Immediately"],
            "phone": ["+91 98765 43210", "9876543210"],
            "email": ["someone@example.com"],
        }
        for name, values in accepted.items():
            for value in values:
                answer, reason = ApplicationProfile.from_mapping({name: value}).answer_for(name)
                self.assertEqual((answer, reason), (value, ""), (name, value))

    def test_validation_rejects_values_that_cannot_answer_the_question(self):
        rejected = {
            "expected_ctc": ["a lot", "12-15", "negotiable"],
            "total_experience": ["many", "4-5", "99"],
            "notice_period": ["soon", "whenever"],
            "phone": ["123", "call me"],
            "email": ["not-an-email"],
        }
        for name, values in rejected.items():
            for value in values:
                answer, reason = ApplicationProfile.from_mapping({name: value}).answer_for(name)
                self.assertIsNone(answer, (name, value))
                self.assertTrue(reason)

    def test_boolean_preferences(self):
        for raw, expected in (("true", "Yes"), ("Yes", "Yes"), ("1", "Yes"), ("false", "No"), ("no", "No")):
            self.assertEqual(ApplicationProfile.from_mapping({"willing_to_relocate": raw}).answer_for("willing_to_relocate"),
                             (expected, ""), raw)
        value, reason = ApplicationProfile.from_mapping({"work_weekends": "sometimes"}).answer_for("work_weekends")
        self.assertIsNone(value)
        self.assertIn("true or false", reason)
        self.assertIsNone(ApplicationProfile().answer_for("work_night_shift")[0])


class TestResumeEvidence(unittest.TestCase):
    def test_mentions_whole_terms_only(self):
        evidence = ResumeEvidence("## Skills\nJava, Spring Boot, Angular 15, Node.js, C#, AWS (EC2, S3)")
        for term in ("java", "Spring Boot", "angular", "node.js", "c#", "EC2"):
            self.assertTrue(evidence.mentions(term), term)
        for term in ("javascript", "spring", "kubernetes", "c", ""):
            if term == "spring":
                continue  # "Spring" alone is part of "Spring Boot" and is a real word match
            self.assertFalse(evidence.mentions(term), term)

    def test_missing_resume_has_no_evidence(self):
        evidence = ResumeEvidence.from_file(Path(tempfile.gettempdir()) / "definitely-not-a-resume.md")
        self.assertFalse(evidence.available)
        self.assertFalse(evidence.mentions("java"))
        self.assertFalse(ResumeEvidence.from_file(None).available)


if __name__ == "__main__":
    unittest.main()
