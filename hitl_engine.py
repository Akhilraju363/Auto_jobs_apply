import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class HITLEngine:
    """Human-in-the-Loop engine for pause/resume execution on manual intervention."""

    def __init__(self):
        self._paused = False
        self._paused_job = None

    def request_human_intervention(self, reason: str, job_details: dict) -> None:
        """Pause execution and wait for human intervention."""
        print("\n" + "=" * 70)
        print("🚨 HUMAN INTERVENTION REQUIRED 🚨")
        print("=" * 70)
        print(f"Reason: {reason}")
        print(f"Job: {job_details.get('title', 'N/A')} @ {job_details.get('company', 'N/A')}")
        print(f"URL: {job_details.get('url', 'N/A')}")
        print(f"Job ID: {job_details.get('id', 'N/A')}")
        print("=" * 70)
        print("Waiting for manual intervention...")
        print("(Complete the CAPTCHA/OTP or required action, then press Enter)")
        print("=" * 70 + "\n")

        logger.warning(
            f"HITL requested: {reason} | Job: {job_details.get('id', 'N/A')} | "
            f"Title: {job_details.get('title', 'N/A')}"
        )

        self._paused = True
        self._paused_job = job_details

        input("Press Enter to resume execution: ")

        self._paused = False
        self._paused_job = None

        print("✓ Resuming execution...\n")
        logger.info("HITL intervention complete, resuming automation")

    def resume_execution(self) -> None:
        """Signal to resume execution (for programmatic control)."""
        self._paused = False
        self._paused_job = None
        logger.info("Execution resumed")

    def is_paused(self) -> bool:
        """Check if currently paused for human intervention."""
        return self._paused

    def get_paused_job(self) -> dict | None:
        """Get the job that triggered the pause."""
        return self._paused_job


__all__ = ["HITLEngine"]
