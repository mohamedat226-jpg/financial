"""Read the PDF text layer as positioned words/lines so every extracted value can be traced
to a page and bounding box. Optional Tesseract OCR for scanned pages if installed."""
from __future__ import annotations

import io
import threading
import shutil
from dataclasses import dataclass, field

import pdfplumber

from .normalize import has_arabic, norm_text, reverse_rtl


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int


@dataclass
class Line:
    page: int
    words: list[Word]
    page_width: float
    page_height: float
    text: str = ""
    norm: str = ""
    norm_rev: str = ""

    def __post_init__(self):
        self.words.sort(key=lambda w: w.x0)
        self.text = " ".join(w.text for w in self.words)
        self.norm = norm_text(self.text)
        self.norm_rev = norm_text(reverse_rtl(self.text)) if has_arabic(self.text) else self.norm

    @property
    def bbox(self):
        return [min(w.x0 for w in self.words), min(w.top for w in self.words),
                max(w.x1 for w in self.words), max(w.bottom for w in self.words)]

    @property
    def top(self):
        return min(w.top for w in self.words)


@dataclass
class DocText:
    pages: int
    lines: list[Line] = field(default_factory=list)
    page_sizes: list[tuple[float, float]] = field(default_factory=list)
    has_text_layer: bool = True
    ocr_used: bool = False

    @property
    def full_text(self) -> str:
        return "\n".join(l.text for l in self.lines)


def _group_lines(words: list[Word], pw: float, ph: float, tol: float = 3.0) -> list[Line]:
    words = sorted(words, key=lambda w: (round(w.top), w.x0))
    lines: list[list[Word]] = []
    for w in words:
        for ln in lines:
            ref = ln[0]
            if abs(((ref.top + ref.bottom) / 2) - ((w.top + w.bottom) / 2)) <= tol:
                ln.append(w)
                break
        else:
            lines.append([w])
    out = [Line(page=ws[0].page, words=ws, page_width=pw, page_height=ph) for ws in lines]
    out.sort(key=lambda l: (l.page, l.top))
    return out


def read_pdf(path: str) -> DocText:
    with pdfplumber.open(path) as pdf:
        doc = DocText(pages=len(pdf.pages))
        for i, page in enumerate(pdf.pages, start=1):
            doc.page_sizes.append((float(page.width), float(page.height)))
            words = [Word(w["text"], float(w["x0"]), float(w["x1"]), float(w["top"]), float(w["bottom"]), i)
                     for w in page.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.5)]
            doc.lines.extend(_group_lines(words, float(page.width), float(page.height)))
    doc.has_text_layer = sum(len(l.text) for l in doc.lines) >= 40
    if not doc.has_text_layer and tesseract_available():
        ocr = _ocr(path)
        if ocr is not None:
            ocr.has_text_layer = False
            return ocr
    return doc


def tesseract_available() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        import pytesseract  # noqa: F401
        return True
    except ImportError:
        return False


# PDFium is not thread-safe: every pypdfium2 call in the application must hold this lock
# (extraction runs on several worker threads alongside page-image requests from the UI).
PDFIUM_LOCK = threading.RLock()


def _ocr(path: str) -> DocText | None:
    import pypdfium2 as pdfium
    import pytesseract
    scale = 300 / 72
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            pages = []
            for i in range(len(pdf)):
                page = pdf[i]
                pages.append((page.get_size(), page.render(scale=scale).to_pil()))
        finally:
            pdf.close()
    doc = DocText(pages=len(pages), ocr_used=True)
    for i, ((pw, ph), img) in enumerate(pages):
        doc.page_sizes.append((pw, ph))
        data = pytesseract.image_to_data(img, lang="ara+eng", output_type=pytesseract.Output.DICT)
        words = []
        for j, txt in enumerate(data["text"]):
            if txt.strip() and float(data["conf"][j]) > 0:
                x, y, w, h = (data[k][j] / scale for k in ("left", "top", "width", "height"))
                words.append(Word(txt, x, x + w, y, y + h, i + 1))
        doc.lines.extend(_group_lines(words, pw, ph, tol=6))
    return doc


def render_page_png(path: str, page_no: int, scale: float = 1.6) -> bytes:
    import pypdfium2 as pdfium
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(path)
        try:
            img = pdf[page_no - 1].render(scale=scale).to_pil()
        finally:
            pdf.close()
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
