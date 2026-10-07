"""Resolve the AI Agent's verified, job-specific resume artifact for a job (Phase 3.2). Read-only.

    job.url -> canonical link -> tailored_jobs.json (resume_id, version) / generated_resumes meta
    -> verified version -> v<n>.md (review) + v<n>.pdf|docx (upload, after validation)

Only verified job-specific resume artifacts may be uploaded to Naukri. The master resume is never
automatically substituted, nothing is downloaded from Drive, and nothing is generated here: the AI
Agent publishes v<n>.pdf (scripts/resume_artifacts.py). This module only checks that a file:
belongs to this job, is the verified version, was rendered from that exact markdown, parses, has
real text, and contains the verified resume's sections and wording.
Never logs resume contents.
"""

import hashlib
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Optional

from ai_agent_bridge import agent_canonical_link

logger = logging.getLogger(__name__)

RESUME_NOT_FOUND = "RESUME_NOT_FOUND"
RESUME_NOT_VERIFIED = "RESUME_NOT_VERIFIED"
RESUME_ARTIFACT_MISMATCH = "RESUME_ARTIFACT_MISMATCH"
RESUME_PDF_NOT_FOUND = "RESUME_PDF_NOT_FOUND"
RESUME_PDF_INVALID = "RESUME_PDF_INVALID"
RESUME_PDF_VALIDATION_FAILED = "RESUME_PDF_VALIDATION_FAILED"

MIN_TEXT_CHARS = 200
MIN_WORD_COVERAGE = 0.9  # share of the verified markdown's significant words found in the file


@dataclass
class ResumeArtifact:
    job_key: str
    resume_id: str
    version: int
    verified: bool
    markdown_path: Path
    pdf_path: Optional[Path] = None
    docx_path: Optional[Path] = None
    drive_link: Optional[str] = None
    upload_path: Optional[Path] = None  # set only for a fully validated PDF/DOCX
    upload_code: Optional[str] = None  # why there is no upload file
    upload_reason: str = ""
    checks: list[str] = field(default_factory=list)

    @property
    def uploadable(self) -> bool:
        return self.upload_path is not None


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _md_sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _job_key(meta: dict) -> Optional[str]:
    job = meta.get("job") or {}
    return agent_canonical_link(job.get("job_key") or job.get("link"))


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 4}


def check_text_consistency(markdown: str, text: str) -> list[str]:
    """Problems if the extracted file text is not the verified markdown (empty list == consistent).
    Tolerates the export's extra fixed header and contact line; catches empty/wrong/old resumes."""
    if len(re.sub(r"\s+", "", text)) < MIN_TEXT_CHARS:
        return ["no meaningful text"]
    problems = []
    flat = re.sub(r"\s+", " ", text.lower())
    missing = [h for h in (line[3:].strip() for line in markdown.splitlines() if line.startswith("## "))
               if re.sub(r"\s+", " ", h.lower()) not in flat]
    if missing:
        problems.append(f"{len(missing)} section heading(s) missing")
    words = _tokens(re.sub(r"[#*_`>\[\]()]", " ", markdown))
    if words:
        coverage = len(words & _tokens(text)) / len(words)
        if coverage < MIN_WORD_COVERAGE:
            problems.append(f"only {coverage:.0%} of the verified resume's wording found")
    return problems


def extract_pdf_text(path: Path) -> str:
    """Text of a PDF; raises ValueError when it is not a readable PDF."""
    with open(path, "rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError("missing %PDF- header")
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if not reader.pages:
            raise ValueError("PDF has no pages")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ValueError:
        raise
    except Exception as e:  # pypdf raises several types for damaged files
        raise ValueError(f"PDF could not be parsed: {type(e).__name__}") from e


def extract_docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise ValueError(f"not a valid DOCX: {type(e).__name__}") from e
    return unescape(" ".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml)))


def validate_resume_document(path: Path, markdown: str) -> tuple[Optional[str], str]:
    """(None, "") when the PDF/DOCX is a readable, meaningful copy of the verified markdown,
    else (code, reason)."""
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return RESUME_PDF_INVALID, f"{path.name} is missing or empty"
        text = extract_pdf_text(path) if path.suffix.lower() == ".pdf" else extract_docx_text(path)
    except (OSError, ValueError) as e:
        return RESUME_PDF_INVALID, f"{path.name}: {e}"
    problems = check_text_consistency(markdown, text)
    if problems:
        return RESUME_PDF_VALIDATION_FAILED, f"{path.name}: " + "; ".join(problems)
    return None, ""


def _find_record(out: Path, key: str, saved: Optional[dict]) -> tuple[Optional[tuple[Path, dict]], Optional[str], str]:
    root = out / "generated_resumes"
    if saved and saved.get("resume_id"):
        folder = root / str(saved["resume_id"])
        meta = _load_json(folder / "meta.json") if re.fullmatch(r"[0-9a-f]{12}", str(saved["resume_id"])) else None
        if not isinstance(meta, dict):
            return None, RESUME_NOT_FOUND, f"resume record {saved['resume_id']} not found"
        if _job_key(meta) != key:
            return None, RESUME_ARTIFACT_MISMATCH, "the linked resume record belongs to a different job"
        return (folder, meta), None, ""
    best = None
    for meta_path in sorted(root.glob("*/meta.json")):
        meta = _load_json(meta_path)
        if isinstance(meta, dict) and _job_key(meta) == key:
            if best is None or str(meta.get("updated_at") or "") > str(best[1].get("updated_at") or ""):
                best = (meta_path.parent, meta)
    if best is None:
        if saved is not None:
            return None, RESUME_NOT_FOUND, ("the AI Agent's tailored resume is only in Drive (no local copy); "
                                            f"download it: {saved.get('resume_link')}")
        return None, RESUME_NOT_FOUND, "the AI Agent has no saved tailored resume for this job"
    return best, None, ""


def resolve_verified_resume(job_url: str, agent_output_dir: Optional[Path]
                            ) -> tuple[Optional[ResumeArtifact], Optional[str], str]:
    """(artifact, None, "") or (None, code, reason). An artifact without upload_path is still a
    valid verified resume for review; its upload_code says why it cannot be uploaded."""
    if agent_output_dir is None:
        return None, RESUME_NOT_FOUND, "AI_AGENT_OUTPUT_DIR is not set"
    out, key = Path(agent_output_dir), agent_canonical_link(job_url)
    tailored = _load_json(out / "tailored_jobs.json") or []
    saved = next((t for t in tailored if isinstance(t, dict) and t.get("status") == "saved"
                  and agent_canonical_link(t.get("link")) == key), None)
    found, code, reason = _find_record(out, key, saved)
    if found is None:
        return None, code, reason
    folder, meta = found
    versions = meta.get("versions") or []
    wanted = (saved or {}).get("resume_version")
    if wanted is not None:
        version = next((v for v in versions if v.get("n") == wanted), None)
    else:
        version = next((v for v in reversed(versions) if (v.get("validation") or {}).get("ok")), None)
    if version is None or not (version.get("validation") or {}).get("ok"):
        return None, RESUME_NOT_VERIFIED, "no version of this resume passed the AI Agent's verification"
    n = version["n"]
    md_path = folder / f"v{n}.md"
    try:
        markdown = md_path.read_text(encoding="utf-8")
    except OSError:
        return None, RESUME_NOT_FOUND, f"verified markdown v{n}.md is missing"
    artifact = ResumeArtifact(key, meta.get("id") or folder.name, n, True, md_path,
                              drive_link=(saved or {}).get("resume_link"))
    _attach_upload_file(artifact, folder, version, markdown, key)
    logger.info(f"Resume artifact | resume_id={artifact.resume_id} | v{n} | verified=yes | "
                f"upload={'yes ' + artifact.upload_path.suffix if artifact.uploadable else artifact.upload_code}")
    return artifact, None, ""


def _attach_upload_file(artifact: ResumeArtifact, folder: Path, version: dict, markdown: str, key: str) -> None:
    n = version["n"]
    artifact.pdf_path = folder / f"v{n}.pdf" if (folder / f"v{n}.pdf").exists() else None
    artifact.docx_path = folder / f"v{n}.docx" if (folder / f"v{n}.docx").exists() else None
    if artifact.pdf_path is None and artifact.docx_path is None:
        artifact.upload_code, artifact.upload_reason = RESUME_PDF_NOT_FOUND, f"no v{n}.pdf/v{n}.docx published by the AI Agent"
        return
    recorded = (version.get("artifacts") or {})
    for path in (artifact.pdf_path, artifact.docx_path):
        if path is None:
            continue
        info = recorded.get(path.suffix[1:])
        if info:  # published by resume_artifacts.py: identity and freshness are recorded
            if info.get("job_key") and agent_canonical_link(info["job_key"]) != key:
                artifact.upload_code, artifact.upload_reason = RESUME_ARTIFACT_MISMATCH, f"{path.name} was made for another job"
                return
            if not info.get("verified") or info.get("version") != n:
                artifact.upload_code, artifact.upload_reason = RESUME_NOT_VERIFIED, f"{path.name} is not the verified v{n}"
                continue
            if info.get("md_sha1") != _md_sha1(markdown):
                artifact.upload_code, artifact.upload_reason = RESUME_PDF_VALIDATION_FAILED, f"{path.name} is older than v{n}.md"
                continue
            artifact.checks.append(f"{path.name}: published for this job from v{n}.md")
        code, reason = validate_resume_document(path, markdown)
        if code:
            artifact.upload_code, artifact.upload_reason = code, reason
            continue
        artifact.checks.append(f"{path.name}: readable, text matches v{n}.md")
        artifact.upload_path, artifact.upload_code, artifact.upload_reason = path, None, ""
        return


__all__ = [
    "RESUME_ARTIFACT_MISMATCH",
    "RESUME_NOT_FOUND",
    "RESUME_NOT_VERIFIED",
    "RESUME_PDF_INVALID",
    "RESUME_PDF_NOT_FOUND",
    "RESUME_PDF_VALIDATION_FAILED",
    "ResumeArtifact",
    "check_text_consistency",
    "resolve_verified_resume",
    "validate_resume_document",
]
