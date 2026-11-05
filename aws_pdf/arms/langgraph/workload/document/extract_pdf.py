"""PDF text extraction. Pure computation — no fastapi, no langgraph imports.

The pypdf version is pinned in pyproject and reported in /meta: it defines
the offline reference, so a version bump changes extraction output and
invalidates parity artifacts.
"""


def extract_pdf(source_path: str) -> str:
    # Imported inside the function so the runtime thread pins applied at
    # startup land before any heavy library initializes.
    from pypdf import PdfReader

    reader = PdfReader(source_path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def pypdf_version() -> str:
    from pypdf import __version__

    return __version__
