"""Phase 3.1: deterministic question classification and answer decisions (never guesses)."""

import unittest

from application_profile import ApplicationProfile, ResumeEvidence
from application_questions import (
    QUESTION_AUTO_ANSWERED,
    QUESTION_MANUAL_REQUIRED,
    UNCONFIGURED_ANSWER,
    QuestionCategory as C,
    classify_question,
    decide_answer,
)

FULL_PROFILE = ApplicationProfile.from_mapping({
    "total_experience": "4", "current_ctc": "8", "expected_ctc": "12", "notice_period": "30 days",
    "current_location": "Hyderabad", "preferred_location": "Pune", "phone": "9876543210",
    "email": "someone@example.com", "willing_to_relocate": "true", "work_from_office": "true",
    "work_night_shift": "false", "work_weekends": "false",
})
EVIDENCE = ResumeEvidence("## Skills\nJava, Spring Boot, Angular, Microservices, Docker, AWS\n## Experience\n...")


class TestClassification(unittest.TestCase):
    CASES = {
        C.EXPECTED_CTC: ["What is your expected CTC?", "What are your salary expectations?", "Expected salary?",
                         "Expected CTC in LPA?", "What is your expected CTC in Lacs per annum?",
                         "What salary are you expecting?", "What is your expected compensation?", "Expected package?",
                         "Expected salary in LPA?"],
        C.CURRENT_CTC: ["What is your current CTC?", "Current salary?", "Current compensation?",
                        "What are you currently earning?", "Current CTC (in LPA)"],
        C.NOTICE_PERIOD: ["How soon can you join?", "What is your notice period?", "When can you join?",
                          "How many days notice do you need?", "Notice period"],
        C.CURRENT_LOCATION: ["Where are you currently located?", "Current location", "What is your current city?"],
        C.PREFERRED_LOCATION: ["Preferred location?", "What is your preferred job location?"],
        C.TOTAL_EXPERIENCE: ["Total experience", "Total years of experience?", "How many years of experience do you have?",
                             "What is your overall experience?"],
        C.PHONE: ["Mobile number", "Contact number", "Phone"],
        C.EMAIL: ["Email address", "E-mail"],
        C.WILLING_TO_RELOCATE: ["Are you willing to relocate?", "Are you open to relocating to Bangalore?"],
        C.WORK_FROM_OFFICE: ["Are you comfortable working from office 5 days a week?", "Is WFO okay for you?",
                             "Are you okay with a hybrid model?"],
        C.WORK_NIGHT_SHIFT: ["Are you okay with night shifts?", "Can you work in rotational shifts?"],
        C.WORK_WEEKENDS: ["Can you work on weekends?", "Are you available on Saturdays?"],
        C.TECHNICAL_SKILL: ["Do you have experience with Spring Boot?", "How many years of experience do you have with Kubernetes?",
                            "Years of experience in Java?", "Rate your Java skills", "Are you familiar with Docker?"],
        C.FREE_TEXT: ["Why do you want to join us?", "Why should we hire you?", "Tell us about yourself.",
                      "Describe your experience.", "Why are you looking for a change?", "What are your career goals?",
                      "Additional information"],
        C.WORK_AUTHORIZATION: ["Are you authorized to work in India?", "Do you require visa sponsorship?",
                               "What is your citizenship?"],
        C.DEMOGRAPHIC: ["What is your gender?", "What is your age?", "Date of birth", "Do you have a disability?",
                        "Are you a veteran?"],
        C.IDENTITY: ["Please share your PAN number", "Aadhaar number"],
        C.LEGAL_DECLARATION: ["Do you agree to the terms?", "Do you have any criminal convictions?",
                              "Do you consent to a background check?", "Do you have any conflict of interest?"],
        C.EDUCATION: ["Highest qualification?", "What is your graduation percentage?"],
        C.EMPLOYMENT_HISTORY: ["What is your current company?", "Previous employer?"],
        C.UNKNOWN: ["Favourite colour?", "", "   ", "Hello"],
    }

    def test_variants(self):
        for category, texts in self.CASES.items():
            for text in texts:
                self.assertEqual(classify_question(text), category, text)

    def test_sensitive_wins_over_answerable_words(self):
        self.assertEqual(classify_question("Are you willing to relocate? Please explain why."), C.FREE_TEXT)
        self.assertEqual(classify_question("Why is your expected CTC higher than market?"), C.FREE_TEXT)
        self.assertEqual(classify_question("Do you agree to share your current CTC slip?"), C.LEGAL_DECLARATION)


class TestDecisions(unittest.TestCase):
    def test_configured_facts_are_answered(self):
        cases = {
            "What is your expected CTC in Lacs per annum?": "12", "Current salary?": "8",
            "How soon can you join?": "30 days", "Total experience": "4", "Current location": "Hyderabad",
            "Preferred location?": "Pune", "Mobile number": "9876543210", "Email": "someone@example.com",
            "Are you willing to relocate?": "Yes", "Can you work on weekends?": "No",
        }
        for text, expected in cases.items():
            decision = decide_answer(text, FULL_PROFILE, EVIDENCE)
            self.assertEqual((decision.value, decision.code, decision.source != ""), (expected, QUESTION_AUTO_ANSWERED, True), text)

    def test_missing_configuration_never_falls_back(self):
        only_current = ApplicationProfile.from_mapping({"current_ctc": "8", "total_experience": "4"})
        for text in ("What is your expected CTC?", "Notice period", "Are you willing to relocate?", "Current location"):
            decision = decide_answer(text, only_current, EVIDENCE)
            self.assertIsNone(decision.value, text)
            self.assertEqual(decision.code, UNCONFIGURED_ANSWER, text)

    def test_options_must_match_exactly(self):
        decision = decide_answer("Notice period", FULL_PROFILE, EVIDENCE, "radio", ["15 days", "30 Days", "60 days"])
        self.assertEqual(decision.value, "30 Days")
        decision = decide_answer("Expected CTC", FULL_PROFILE, EVIDENCE, "radio", ["5-8 LPA", "8-12 LPA"])
        self.assertIsNone(decision.value)
        self.assertEqual(decision.code, UNCONFIGURED_ANSWER)
        self.assertEqual(decide_answer("Are you willing to relocate?", FULL_PROFILE, EVIDENCE, "radio", ["Yes", "No"]).value, "Yes")

    def test_technical_skills(self):
        self.assertEqual(decide_answer("Do you have experience with Spring Boot?", FULL_PROFILE, EVIDENCE).value, "Yes")
        self.assertEqual(decide_answer("Do you have hands-on experience in Java and Angular?", FULL_PROFILE, EVIDENCE).value, "Yes")
        manual = [
            "Do you have experience with Kubernetes?",          # not in the master resume -> never "No"
            "Do you have experience in Java or Kotlin?",         # ambiguous "or"
            "How many years of experience do you have with Spring Boot?",  # duration never derived
            "How many years of Angular experience do you have?",
            "Rate your Java skills out of 10",
        ]
        for text in manual:
            decision = decide_answer(text, FULL_PROFILE, EVIDENCE)
            self.assertIsNone(decision.value, text)
            self.assertEqual(decision.code, QUESTION_MANUAL_REQUIRED, text)
        self.assertIsNone(decide_answer("Do you have experience with Java?", FULL_PROFILE, ResumeEvidence()).value)

    def test_unknown_sensitive_and_free_text_never_get_an_answer(self):
        never = [t for cat, texts in TestClassification.CASES.items()
                 if cat in (C.FREE_TEXT, C.WORK_AUTHORIZATION, C.DEMOGRAPHIC, C.IDENTITY, C.LEGAL_DECLARATION,
                            C.EDUCATION, C.EMPLOYMENT_HISTORY, C.UNKNOWN) for t in texts]
        for text in never:
            for kind, options in (("text", None), ("radio", ["Yes", "No"])):
                decision = decide_answer(text, FULL_PROFILE, EVIDENCE, kind, options)
                self.assertIsNone(decision.value, text)
                self.assertFalse(decision.automatic, text)

    def test_free_text_inputs_and_checkboxes_are_manual(self):
        for kind in ("textarea", "checkbox", "file"):
            self.assertIsNone(decide_answer("Expected CTC", FULL_PROFILE, EVIDENCE, kind).value, kind)


if __name__ == "__main__":
    unittest.main()
