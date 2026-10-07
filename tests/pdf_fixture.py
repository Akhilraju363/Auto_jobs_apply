"""Test helpers: a realistic resume markdown and a small, real PDF with extractable text."""

import zipfile
from pathlib import Path

RESUME_MD = """# Test Candidate

## Summary
Java full stack engineer building Spring Boot microservices and Angular applications for enterprise clients.

## Skills
- Languages: Java, TypeScript, JavaScript, SQL
- Frameworks: Spring Boot, Hibernate, Angular, React
- Cloud and tools: AWS, Docker, Kubernetes, Jenkins, Git

## Experience
### Software Engineer, Example Systems (2021 - Present)
- Designed REST APIs and microservices with Spring Boot and PostgreSQL
- Built Angular dashboards consumed by operations teams
- Automated deployments with Jenkins pipelines and Docker containers

## Education
- Bachelor of Technology, Computer Science
"""


def markdown_lines(markdown: str) -> list[str]:
    return [line.lstrip("#- ").strip() for line in markdown.splitlines() if line.strip()]


def make_pdf(lines: list[str]) -> bytes:
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    content = "BT /F1 10 Tf 40 800 Td 13 TL " + " ".join(f"({esc(line)}) '" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    out = b"%PDF-1.4\n%" + b"x" * 900 + b"\n"
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def write_pdf_for(path: Path, markdown: str = RESUME_MD, extra_lines=()) -> Path:
    path.write_bytes(make_pdf(["Test Candidate | contact line"] + markdown_lines(markdown) + list(extra_lines)))
    return path


def write_docx_for(path: Path, markdown: str = RESUME_MD) -> Path:
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in markdown_lines(markdown))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", f"<w:document><w:body>{body}</w:body></w:document>")
    return path
