from typing import Optional
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from config import RESUME_PATH, MATCH_THRESHOLD


class ResumeMatcher:
    def __init__(self, resume_path: str = str(RESUME_PATH)):
        self.resume_path = resume_path
        self.resume_text = self._load_resume()
        self.vectorizer = TfidfVectorizer(lowercase=True, stop_words="english")

    def _load_resume(self) -> str:
        try:
            with open(self.resume_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def calculate_match_score(self, job_description: str) -> float:
        if not self.resume_text or not job_description:
            return 0.0

        documents = [self.resume_text, job_description]
        tfidf_matrix = self.vectorizer.fit_transform(documents)
        similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])
        score = float(similarity[0][0]) * 100
        return round(score, 2)

    def is_match(self, job_description: str, threshold: float = MATCH_THRESHOLD) -> bool:
        return self.calculate_match_score(job_description) >= threshold

    def batch_score(self, job_descriptions: list[str]) -> list[float]:
        return [self.calculate_match_score(jd) for jd in job_descriptions]


__all__ = ["ResumeMatcher"]
