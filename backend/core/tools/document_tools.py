"""Workspace-confined document extraction adapters.

The model sees one stable ``document_open`` contract.  Format-specific libraries
remain implementation details and every result uses the generic spill protocol
when it exceeds the normal tool-result budget.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import sys
from pathlib import Path

from backend.core.errors import error_payload
from backend.core.tools.workspace_tools import _allowed_roots, _confined_path, _rel, _workspace
from .document_limits import check_input, bounded_join, DocumentLimit, MAX_TEXT_CHARS


def _document_error(name: str, *, message: str = "", details: dict | None = None,
                    next_actions: list[dict] | None = None) -> dict:
    return error_payload(
        name, message=message, details=details, next_actions=next_actions,
    )


def document_open(args: dict) -> dict:
    workspace = _workspace(args)
    path = _confined_path(
        workspace, args.get("path", ""), must_exist=True,
        allowed_roots=_allowed_roots(args),
    )
    suffix = path.suffix.lower()
    start = max(0, int(args.get("offset", 0) or 0))
    limit = max(1_000, min(int(args.get("max_chars", 24_000) or 24_000), 100_000))
    try:
        check_input(path)
        if suffix in {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml"}:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        elif suffix == ".pdf":
            from pypdf import PdfReader
            pages = PdfReader(str(path)).pages
            if len(pages) > 500:
                raise DocumentLimit("PDF exceeds 500 pages; split it before extraction")
            text = bounded_join(((page.extract_text() or "") for page in pages), "\n\n")
        elif suffix == ".docx":
            from docx import Document
            doc = Document(str(path))
            def docx_chunks():
                yield from (paragraph.text for paragraph in doc.paragraphs)
                for table in doc.tables:
                    for row in table.rows:
                        yield bounded_join((cell.text for cell in row.cells), "\t")
            text = bounded_join(docx_chunks())
        elif suffix in {".pptx"}:
            from pptx import Presentation
            deck = Presentation(str(path))
            if len(deck.slides) > 500:
                raise DocumentLimit("presentation exceeds 500 slides")
            chunks = []
            for number, slide in enumerate(deck.slides, 1):
                chunks.append(f"[Slide {number}]")
                chunks.extend(
                    str(shape.text) for shape in slide.shapes
                    if hasattr(shape, "text") and str(shape.text).strip()
                )
            text = bounded_join(chunks)
        elif suffix in {".xlsx", ".xlsm"}:
            from openpyxl import load_workbook
            book = load_workbook(str(path), read_only=True, data_only=True)
            try:
                def sheet_chunks():
                    for sheet in book.worksheets:
                        if (sheet.max_row or 0) * (sheet.max_column or 0) > 200_000:
                            raise DocumentLimit("worksheet exceeds 200000 cells")
                        yield f"[Sheet {sheet.title}]"
                        for row in sheet.iter_rows(values_only=True):
                            yield bounded_join(("" if value is None else str(value) for value in row), "\t")
                text = bounded_join(sheet_chunks())
            finally:
                book.close()
        elif suffix == ".xls":
            import xlrd
            book = xlrd.open_workbook(str(path), on_demand=True)
            try:
                chunks = []
                for sheet in book.sheets():
                    if sheet.nrows * sheet.ncols > 200_000:
                        raise DocumentLimit("worksheet exceeds 200000 cells")
                    chunks.append(f"[Sheet {sheet.name}]")
                    for row in range(sheet.nrows):
                        chunks.append("\t".join(str(sheet.cell_value(row, col)) for col in range(sheet.ncols)))
                text = bounded_join(chunks)
            finally:
                book.release_resources()
        elif suffix == ".doc":
            from .document_limits import verified_antiword, check_converter
            try:
                antiword, expected = verified_antiword(workspace)
                check_converter(antiword, expected)
            except ValueError as exc:
                return _document_error(
                    "DOCUMENT_FORMAT_UNSUPPORTED",
                    message=str(exc),
                    details={"format": "doc", "path": _rel(workspace, path),
                             "missing_adapter": "antiword"},
                    next_actions=[{"action": "convert", "format": "docx"},
                                  {"action": "install_adapter", "adapter": "antiword"}],
                )
            from backend.core.process_control import creation_flags, attach_kill_job, terminate_tree, close_job
            from backend.core.process_output import collect_output, OutputLimit
            from backend.core.tools.execution_profile import safe_environment
            from backend.core.installation_lease import child_options
            completed = subprocess.Popen([str(antiword), str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                env=safe_environment(), creationflags=creation_flags(), start_new_session=os.name != "nt", **child_options())
            job = attach_kill_job(completed)
            try:
                stdout, stderr, _ = collect_output(completed, timeout=60, limit=MAX_TEXT_CHARS,
                    reject_overflow=True, terminate=lambda: terminate_tree(completed, job))
            except OutputLimit as exc:
                raise DocumentLimit(str(exc)) from exc
            finally:
                close_job(job)
            if completed.returncode != 0:
                return _document_error(
                    "DOCUMENT_READ_FAILED",
                    message="The .doc converter could not read this document.",
                    details={"format": "doc", "path": _rel(workspace, path),
                             "diagnostic": stderr[:1000]},
                )
            text = stdout
        elif suffix == ".ppt":
            return _document_error(
                "DOCUMENT_FORMAT_UNSUPPORTED",
                message="Legacy .ppt requires conversion before extraction.",
                details={"format": "ppt", "path": _rel(workspace, path)},
                next_actions=[{"action": "convert", "format": "pptx"}],
            )
        else:
            return _document_error(
                "DOCUMENT_FORMAT_UNSUPPORTED",
                details={"format": suffix, "path": _rel(workspace, path),
                         "supported": ["txt", "md", "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx"]},
                next_actions=[{"action": "convert_to_supported_format"}],
            )
        if len(text) > MAX_TEXT_CHARS:
            raise DocumentLimit("extracted document exceeds 2000000 characters")
    except DocumentLimit as exc:
        return _document_error("DOCUMENT_READ_FAILED", message=str(exc),
            details={"code": "DOCUMENT_RESOURCE_LIMIT", "path": _rel(workspace, path)},
            next_actions=[{"action": "split_or_convert_document"}])
    except ImportError as exc:
        return _document_error(
            "DOCUMENT_FORMAT_UNSUPPORTED",
            message="The document adapter is not installed in the Gitgo runtime.",
            details={"format": suffix, "dependency": str(exc),
                     "path": _rel(workspace, path)},
            next_actions=[{"action": "repair_portable_runtime"}],
        )
    except Exception as exc:
        return _document_error(
            "DOCUMENT_READ_FAILED", message=str(exc)[:2000],
            details={"format": suffix, "path": _rel(workspace, path)},
            next_actions=[{"action": "inspect_or_convert_document"}],
        )
    end = min(len(text), start + limit)
    return {
        "path": _rel(workspace, path), "format": suffix.removeprefix("."),
        "content": text[start:end], "offset": start,
        "next_offset": end if end < len(text) else None,
        "total_chars": len(text), "truncated": end < len(text),
    }


def _prepare_output(args: dict) -> tuple[Path, Path, bool, str]:
    workspace = _workspace(args)
    path = _confined_path(
        workspace, args.get("path", ""), must_exist=False,
        allowed_roots=_allowed_roots(args),
    )
    existed = path.exists()
    expected = str(args.get("expected_sha256") or "")
    if existed:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if not bool(args.get("overwrite", False)):
            raise ValueError("FILE_EXISTS: set overwrite=true and provide expected_sha256")
        if not expected:
            raise ValueError("EXPECTED_HASH_REQUIRED")
        if expected != current:
            raise ValueError(f"FILE_CHANGED: expected {expected}, found {current}")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=path.suffix, dir=str(path.parent),
    )
    os.close(handle)
    return workspace, path, existed, temporary


def _content_lines(content: str) -> list[str]:
    return content.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def document_create(args: dict) -> dict:
    from .document_service import document_create as create
    return create(args)


def render_document(args: dict) -> dict:
    """Internal native producer; Task Runtime supplies a private workspace.

    This handler is not a model-visible tool or a formal file commit service.
    Third-party format processing stays inside the native process boundary.
    """
    workspace = None
    path = None
    temporary = ""
    try:
        workspace, path, existed, temporary = _prepare_output(args)
        suffix = path.suffix.lower()
        title = str(args.get("title") or path.stem)
        content = str(args.get("content") or "")
        if suffix in {".txt", ".md", ".markdown"}:
            Path(temporary).write_text(content, encoding="utf-8")
        elif suffix == ".docx":
            from docx import Document
            document = Document()
            if title:
                document.add_heading(title, level=1)
            for line in _content_lines(content):
                stripped = line.strip()
                if stripped.startswith("# "):
                    document.add_heading(stripped[2:], level=1)
                elif stripped.startswith("## "):
                    document.add_heading(stripped[3:], level=2)
                elif stripped.startswith(("- ", "* ")):
                    document.add_paragraph(stripped[2:], style="List Bullet")
                else:
                    document.add_paragraph(line)
            document.save(temporary)
        elif suffix == ".pptx":
            from pptx import Presentation
            deck = Presentation()
            slides = list(args.get("slides") or [])
            if not slides:
                chunks = content.split("\n---\n") if content else [""]
                slides = [{"title": title if index == 0 else f"{title} {index + 1}", "body": chunk}
                          for index, chunk in enumerate(chunks)]
            for raw in slides:
                item = dict(raw or {})
                slide = deck.slides.add_slide(deck.slide_layouts[1])
                slide.shapes.title.text = str(item.get("title") or "")
                slide.placeholders[1].text = str(item.get("body") or "")
            deck.save(temporary)
        elif suffix in {".xlsx", ".xlsm"}:
            from openpyxl import Workbook
            book = Workbook()
            default = book.active
            book.remove(default)
            sheets = list(args.get("sheets") or [])
            if not sheets:
                rows = [line.split("\t") for line in _content_lines(content)]
                sheets = [{"name": title or "Sheet1", "rows": rows}]
            for index, raw in enumerate(sheets):
                item = dict(raw or {})
                name = str(item.get("name") or f"Sheet{index + 1}")[:31]
                sheet = book.create_sheet(name)
                for row in list(item.get("rows") or []):
                    sheet.append(list(row) if isinstance(row, list) else [row])
            book.save(temporary)
        elif suffix == ".pdf":
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.lib.enums import TA_LEFT
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from xml.sax.saxutils import escape
            styles = getSampleStyleSheet()
            font_name = "Helvetica"
            font_candidates = (
                Path("C:/Windows/Fonts/msyh.ttc"),
                Path("C:/Windows/Fonts/simhei.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            )
            for candidate in font_candidates:
                if not candidate.is_file():
                    continue
                try:
                    pdfmetrics.registerFont(TTFont("GitgoUnicode", str(candidate)))
                    font_name = "GitgoUnicode"
                    break
                except Exception:
                    continue
            story = []
            if title:
                styles["Title"].fontName = font_name
                story.extend([Paragraph(escape(title), styles["Title"]), Spacer(1, 10)])
            body = styles["BodyText"]
            body.fontName = font_name
            body.alignment = TA_LEFT
            for line in _content_lines(content):
                story.append(Paragraph(escape(line) or "&nbsp;", body))
            SimpleDocTemplate(temporary, pagesize=A4).build(story)
        else:
            return _document_error(
                "DOCUMENT_FORMAT_UNSUPPORTED",
                details={"format": suffix, "supported_output": ["txt", "md", "docx", "pptx", "xlsx", "pdf"]},
                next_actions=[{"action": "choose_supported_output_format"}],
            )
        os.replace(temporary, path)
        temporary = ""
        raw = path.read_bytes()
        return {
            "path": _rel(workspace, path),
            "format": suffix.removeprefix("."),
            "action": "updated" if existed else "created",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
    except ImportError as exc:
        return _document_error(
            "DOCUMENT_FORMAT_UNSUPPORTED",
            message="The output adapter is not installed in the Gitgo runtime.",
            details={"dependency": str(exc), "path": str(args.get("path") or "")},
            next_actions=[{"action": "repair_portable_runtime"}],
        )
    except (OSError, ValueError, TypeError) as exc:
        return _document_error(
            "DOCUMENT_WRITE_FAILED",
            message=str(exc)[:2000],
            details={"operation": "create", "path": str(args.get("path") or "")},
            next_actions=[{"action": "correct_output_specification"}],
        )
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
