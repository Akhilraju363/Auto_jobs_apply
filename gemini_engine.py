import re
import logging
try:
    import google.genai as genai
except ImportError:
    import google.generativeai as genai
from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

HUMANIZED_SYSTEM_PROMPT = """You are a software candidate answering application screening form questions based strictly on the provided resume.

TONE & RULES:
- Answer in first-person perspective: "I built...", "My experience with Go includes..."
- Be direct and concise: 2-4 sentences maximum unless asked for an essay.
- Sound like a practical software engineer—not corporate AI.
- BANNED WORDS: spearheaded, testament, delve, thrilled, passionate, leverage, in conclusion, furthermore, beacon, cutting-edge.
- Never lie or fabricate skills not in the resume.
- Output only the final answer. No preamble, no quote wrappers, no conversational filler.
"""


class GeminiEngine:
    def __init__(self, api_key: str = GEMINI_API_KEY):
        if not api_key or api_key == "your_gemini_api_key_here":
            raise ValueError("GEMINI_API_KEY not configured. Please set a valid API key in .env file")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel("gemini-2.5-flash")

    def generate_screening_questions(
        self, job_description: str, resume: str, count: int = 3
    ) -> list[str]:
        prompt = f"""
Given the following job description and resume, generate {count} custom screening questions
that would be most relevant for evaluating the candidate's fit for this role.

Job Description:
{job_description}

Resume:
{resume}

Return only the questions, one per line, numbered 1-{count}.
"""
        response = self.model.generate_content(prompt)
        questions = [q.strip() for q in response.text.split("\n") if q.strip()]
        return questions[:count]

    def evaluate_fit(
        self, job_description: str, resume: str, experience: str
    ) -> dict:
        prompt = f"""
Evaluate the candidate's fit for this role based on the job description, resume, and work experience.
Provide a JSON response with:
- fit_score (0-100)
- strengths (list of 2-3 key strengths)
- gaps (list of 2-3 skill gaps)
- recommendation (hire/review/pass)

Job Description:
{job_description}

Resume:
{resume}

Experience Summary:
{experience}

Return valid JSON only, no markdown formatting.
"""
        response = self.model.generate_content(prompt)
        import json

        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {
                "fit_score": 0,
                "strengths": [],
                "gaps": [],
                "recommendation": "pass",
            }

    def generate_cover_letter(
        self, job_description: str, company: str, position: str
    ) -> str:
        prompt = f"""
Generate a professional, concise cover letter for the following job opportunity.
Keep it to 2-3 paragraphs.

Company: {company}
Position: {position}
Job Description:
{job_description}
"""
        response = self.model.generate_content(prompt)
        return response.text

    def answer_screening_question(self, resume_text: str, question_text: str) -> str:
        if not resume_text or not question_text:
            return ""

        if is_numeric_question(question_text):
            return ""

        try:
            prompt = f"""{HUMANIZED_SYSTEM_PROMPT}

Resume:
{resume_text}

Question:
{question_text}

Answer the question based only on the resume provided."""

            response = self.model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=500,
                ),
            )
            return response.text.strip() if response.text else ""
        except Exception as e:
            logger.error(f"Error generating answer: {e}")
            return ""


def is_numeric_question(question_text: str) -> bool:
    numeric_keywords = [
        r"\byears?\s+of\s+experience\b",
        r"\bnotice\s+period\b",
        r"\bsalary\b",
        r"\bctc\b",
        r"\bcompensation\b",
        r"\bcost\s+to\s+company\b",
        r"\bhow\s+many\s+years\b",
        r"\bnumber\s+of\s+years\b",
        r"\bmonths?\s+of\s+notice\b",
    ]
    question_lower = question_text.lower()
    return any(re.search(pattern, question_lower) for pattern in numeric_keywords)


__all__ = ["GeminiEngine", "is_numeric_question", "HUMANIZED_SYSTEM_PROMPT"]
