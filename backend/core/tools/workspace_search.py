"""Bounded search adapter shared by file discovery and content search.

Leaf tools own resource permission. This adapter owns executable selection,
stream limits, scope validation and honest coverage metadata. It never runs a
shell, downloads an executable or silently changes regex/ignore semantics.
"""
from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
import fnmatch
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from backend.core.process_control import attach_kill_job, close_job, creation_flags, terminate_tree

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 200_000


@dataclass
class StreamOutcome:
    exit_code: int | None = None
    stopped: str = ""
    stderr: str = ""


def stream_process(argv, cwd, separator, consume, deadline):
    """Drain both pipes concurrently, with bounded queues and record sizes."""
    proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=creation_flags(), start_new_session=os.name != "nt")
    job = attach_kill_job(proc)
    chunks = queue.Queue(maxsize=4)
    stop = threading.Event()
    errors = bytearray()
    outcome = StreamOutcome()

    def stdout_reader():
        try:
            while not stop.is_set():
                raw = proc.stdout.read1(8192)
                if not raw:
                    break
                while not stop.is_set():
                    try:
                        chunks.put(raw, timeout=.05)
                        break
                    except queue.Full:
                        pass
        except OSError as exc:
            outcome.stopped = "pipe_error"
            errors.extend(str(exc).encode("utf-8")[:1000])
        finally:
            while not stop.is_set():
                try:
                    chunks.put(None, timeout=.05)
                    break
                except queue.Full:
                    pass

    def stderr_reader():
        try:
            while raw := proc.stderr.read1(4096):
                errors.extend(raw[:max(0, 8192 - len(errors))])
        except OSError:
            pass

    readers = [threading.Thread(target=stdout_reader, daemon=True), threading.Thread(target=stderr_reader, daemon=True)]
    for reader in readers:
        reader.start()
    buffer, seen = bytearray(), 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome.stopped = "timeout"
                break
            try:
                chunk = chunks.get(timeout=min(.1, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                if buffer and not consume(bytes(buffer)):
                    outcome.stopped = "page_limit"
                break
            seen += len(chunk)
            if seen > MAX_STREAM_BYTES:
                outcome.stopped = "stream_budget"
                break
            buffer.extend(chunk)
            while (end := buffer.find(separator)) >= 0:
                if end > MAX_RECORD_BYTES:
                    outcome.stopped = "record_budget"
                    break
                record = bytes(buffer[:end])
                del buffer[:end + len(separator)]
                if record and not consume(record):
                    outcome.stopped = "page_limit"
                    break
            if outcome.stopped:
                break
            if len(buffer) > MAX_RECORD_BYTES:
                outcome.stopped = "record_budget"
                break
        if outcome.stopped:
            terminate_tree(proc, job)
            job = None
        else:
            try:
                outcome.exit_code = proc.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                outcome.stopped = "timeout"
                terminate_tree(proc, job)
                job = None
    finally:
        stop.set()
        if proc.poll() is None:
            terminate_tree(proc, job)
            job = None
        close_job(job)
        for reader in readers:
            reader.join(timeout=1)
        proc.stdout.close()
        proc.stderr.close()
    outcome.stderr = errors[:8192].decode("utf-8", errors="replace")
    return outcome


def resolve_ripgrep():
    # Host-owned override supports packaged runtime integration. No tool input
    # or workspace file can nominate an executable.
    configured = os.environ.get("GITGO_RIPGREP_PATH")
    if configured:
        path = Path(configured)
        return str(path.resolve()) if path.is_absolute() and path.is_file() else None
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).resolve().parent / ("rg.exe" if os.name == "nt" else "rg")
        if bundled.is_file():
            return str(bundled)
    # Windows executable lookup can search cwd before PATH. A repository's
    # rg.exe must never become the engine just because a task runs there.
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(raw.strip('"'))
        if not directory.is_absolute():
            continue
        path = directory / ("rg.exe" if os.name == "nt" else "rg")
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    return None


@dataclass
class SearchPage:
    key: str
    offset: int
    limit: int
    rows: list = field(default_factory=list)
    observed: int = 0
    size: int = 0
    more: bool = False
    reason: str = ""
    warnings: dict = field(default_factory=dict)
    scope: dict = field(default_factory=dict)
    boundary: Path | None = None
    target: Path | None = None

    def warn(self, code, message):
        self.warnings.setdefault(code, {"code": code, "message": message})

    def add(self, row):
        self.observed += 1
        if self.observed <= self.offset:
            return True
        size = len(json.dumps(row, ensure_ascii=True).encode("utf-8"))
        if len(self.rows) >= self.limit or (self.rows and self.size + size > MAX_RESULT_BYTES):
            self.more = True
            self.reason = "result_limit" if len(self.rows) >= self.limit else "output_budget"
            return False
        self.rows.append(row)
        self.size += size
        return True

    def result(self, engine, partial=False):
        next_offset = self.offset + len(self.rows) if self.more else None
        if next_offset is not None and next_offset > 10000:
            next_offset = None
            self.warn("SEARCH_PAGE_LIMIT", "继续分页将超过支持的范围；请缩小 path/include 后重新搜索。")
        warnings = list(self.warnings.values())
        return {self.key: self.rows, "count": len(self.rows), "offset": self.offset,
                "truncated": self.more or partial, "complete": not (self.more or partial),
                "partial": partial, "next_offset": next_offset,
                "truncation_reason": self.reason or None, "engine": engine,
                "degraded": engine == "python", "warnings": warnings,
                "scope": self.scope}


def _number(args, name, default, maximum, minimum=0):
    value = args.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _failure(code, detail, **facts):
    from backend.core.errors import error_payload
    return {**facts, **error_payload(code, message=detail, next_actions=[{
        "action": "search_text", "effect": "Inspect the declared scope; narrow path/include, repair the pattern or explicitly use literal mode."
    }]), "detail": detail, "complete": False, "partial": True}


def search(args, workspace, root, ignored_dirs, *, listing=False):
    """Return stable, compatible rows plus explicit coverage/recovery facts."""
    mode = "files" if listing else args.get("output_mode", "content")
    if not isinstance(mode, str) or mode not in {"content", "files", "count"}:
        return _failure("INVALID_SEARCH_ARGUMENTS", "invalid output_mode")
    try:
        offset = _number(args, "offset", 0, 10000)
        limit = _number(args, "max_results", 500 if listing else 200, 5000 if listing else 2000, 1)
        context = _number(args, "context_lines", 0, 5)
        timeout = _number(args, "timeout", 20, 30, 1)
        includes = [args.get("pattern", "**/*")] if listing else args.get("include", [])
        excludes = args.get("exclude", [])
        if not isinstance(includes, list) or not isinstance(excludes, list) or any(not isinstance(p, str) or not p or p.startswith("!") for p in includes + excludes):
            raise ValueError("include/exclude must be arrays of positive globs")
        if len(includes + excludes) > 64 or sum(len(p) for p in includes + excludes) > 8192:
            raise ValueError("search supports at most 64 globs with an 8KB combined size")
        for name in ("literal", "case_sensitive", "include_hidden", "respect_ignore"):
            if name in args and type(args[name]) is not bool:
                raise ValueError(name + " must be a boolean")
        pattern = "" if listing else args.get("pattern", "")
        if not listing and (not isinstance(pattern, str) or not pattern or len(pattern.encode("utf-8")) > 8192 or "\n" in pattern or "\r" in pattern):
            raise ValueError("pattern must be nonempty, single-line and at most 8KB")
    except (TypeError, ValueError) as exc:
        return _failure("INVALID_SEARCH_ARGUMENTS", str(exc))
    key = "files" if mode == "files" else "counts" if mode == "count" else "matches"
    page = SearchPage(key, offset, limit)
    page.boundary = root if root.is_dir() else root.parent
    page.target = None if root.is_dir() else root
    page.scope = {"path": _relative(workspace, root), "include_hidden": args.get("include_hidden", False),
                  "respect_ignore": args.get("respect_ignore", True), "include": includes, "exclude": excludes,
                  "generated_exclusions": sorted(ignored_dirs), "symlinks": "not followed",
                  "max_file_bytes": None if listing else MAX_FILE_BYTES}
    deadline = time.monotonic() + timeout
    rg = resolve_ripgrep()
    if rg:
        try:
            result = _ripgrep(rg, args, workspace, root, ignored_dirs, listing, mode, pattern, includes, excludes, context, page, deadline)
            if result is not None:
                return result
        except (OSError, subprocess.SubprocessError) as exc:
            page.warn("RIPGREP_UNAVAILABLE", "ripgrep 无法启动；尝试有明确限制的本地回退。" + str(exc)[:300])
    else:
        page.warn("RIPGREP_UNAVAILABLE", "未找到可用 ripgrep；使用有明确限制的本地回退。可安装 rg 或由 Host 设置 GITGO_RIPGREP_PATH。")
    page.warn("SEARCH_FALLBACK", "本次使用 Python 回退；正则、高级 glob 和忽略文件需要 ripgrep，不能把无法覆盖的范围视为没有匹配。")
    return _fallback(args, workspace, root, ignored_dirs, listing, mode, pattern, includes, excludes, context, page, deadline)


def _relative(workspace, path):
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return str(path)


def _checked_path(raw, root, page):
    boundary = page.boundary or (root if root.is_dir() else root.parent)
    path = Path(raw)
    if not path.is_absolute():
        path = boundary / path
    resolved = path.resolve()
    try:
        resolved.relative_to(boundary)
    except ValueError:
        page.warn("SEARCH_SCOPE_SKIPPED", "发现超出搜索范围的链接，已跳过。")
        return None
    if path.is_symlink() or not resolved.is_file():
        page.warn("SEARCH_FILE_UNAVAILABLE", "部分结果文件已变化或是符号链接，未作为可靠匹配返回。")
        return None
    target = page.target or (root if page.boundary is None and root.is_file() else None)
    if target is not None and resolved != target:
        page.warn("SEARCH_SCOPE_SKIPPED", "搜索引擎返回了指定文件之外的结果，已拒绝。")
        return None
    return resolved


def _decode_field(field, *, path=False):
    if "text" in field:
        return field["text"]
    raw = base64.b64decode(field["bytes"], validate=True)
    return os.fsdecode(raw) if path else raw.decode("utf-8", errors="replace")


def _file_row(workspace, path):
    stat = path.stat()
    return {"path": _relative(workspace, path), "size": stat.st_size, "modified_ns": stat.st_mtime_ns}


def _ripgrep(rg, args, workspace, root, ignored, listing, mode, pattern, includes, excludes, context, page, deadline):
    command = [rg, "--no-config", "--no-ignore-parent", "--no-ignore-global", "--sort=path", "--threads=1", "--color=never"]
    if args.get("include_hidden", False):
        command.append("--hidden")
    if args.get("respect_ignore", True) is False:
        command.append("--no-ignore")
    for glob in includes:
        command.append("--glob=" + glob)
    for glob in [*excludes, *(f"**/{d}/**" for d in sorted(ignored))]:
        command.append("--glob=!" + glob)
    if not args.get("include_hidden", False):
        # Positive rg globs implicitly override its hidden-file filter. The
        # explicit Host option remains authoritative in both engines.
        command.extend(["--glob=!**/.*", "--glob=!**/.*/**"])
    if listing:
        command.extend(["--files", "--null"])
    elif mode == "files":
        command.extend(["--files-with-matches", "--null", f"--max-filesize={MAX_FILE_BYTES}"])
    else:
        command.extend(["--json", f"--max-filesize={MAX_FILE_BYTES}"])
        if context and mode == "content":
            command.append(f"--context={context}")
    if not listing:
        if args.get("literal", False):
            command.append("--fixed-strings")
        if not args.get("case_sensitive", False):
            command.append("--ignore-case")
        command.extend(["-e", pattern])
    command.extend(["--", root.name if page.target is not None else "."])
    recent = deque(maxlen=context)
    current_file, matched_count = "", 0

    def consume(raw):
        nonlocal current_file, matched_count
        try:
            if listing or mode == "files":
                path = _checked_path(os.fsdecode(raw), root, page)
                return True if path is None else page.add(_file_row(workspace, path))
            event = json.loads(raw)
            if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                raise ValueError("invalid search event object")
            kind, data = event.get("type"), event.get("data", {})
            if kind not in {"match", "context", "end"}:
                return True
            path = _checked_path(_decode_field(data["path"], path=True), root, page)
            if path is None:
                if mode == "count" and kind == "end":
                    matched_count = 0
                return True
            file = _relative(workspace, path)
            if mode == "count":
                if kind == "match":
                    current_file, matched_count = file, matched_count + 1
                if kind == "end" and matched_count:
                    row = {"file": current_file, "count": matched_count}
                    matched_count = 0
                    return page.add(row)
                return True
            if kind == "end":
                return True
            line = int(data["line_number"])
            text = _decode_field(data["lines"]).rstrip("\r\n")
            if file != current_file:
                recent.clear()
                current_file = file
            if context:
                for row in page.rows[-(context + 1):]:
                    if row["file"] == file and row["line"] < line <= row["line"] + context:
                        row["context_after"].append({"line": line, "text": text[:500]})
                        page.size += min(len(text.encode("utf-8")), 2000) + 50
            keep = True
            if kind == "match":
                row = {"file": file, "line": line, "text": text[:500], "text_truncated": len(text) > 500}
                if context:
                    row.update(context_before=[r for r in recent if line - context <= r["line"] < line], context_after=[])
                keep = page.add(row)
            recent.append({"line": line, "text": text[:500]})
            return keep
        except (KeyError, TypeError, ValueError, OSError) as exc:
            page.warn("SEARCH_RESULT_INVALID", "搜索结果无法验证，覆盖范围不完整：" + str(exc)[:200])
            page.reason = "invalid_result"
            return False

    outcome = stream_process(command, page.boundary, b"\0" if listing or mode == "files" else b"\n", consume, deadline)
    if outcome.stopped == "page_limit" and page.more:
        return page.result("ripgrep")
    if outcome.stopped:
        page.reason = page.reason or outcome.stopped
        page.warn("SEARCH_INCOMPLETE", "搜索达到时间或资源边界，结果不完整；请缩小 path/include 后重试。")
        result = page.result("ripgrep", partial=True)
        if not page.rows:
            return _failure("SEARCH_INCOMPLETE", page.reason, **result)
        return result
    if outcome.exit_code not in (0, 1):
        invalid = "regex parse error" in outcome.stderr or "error parsing regex" in outcome.stderr
        code = "INVALID_REGEX" if invalid else "SEARCH_ERROR"
        page.warn(code, "ripgrep 搜索失败；已保留错误，未切换搜索语义。" + outcome.stderr[:1000])
        return _failure(code, outcome.stderr[:1000], **page.result("ripgrep", partial=True))
    if outcome.stderr.strip():
        page.warn("SEARCH_INCOMPLETE", "部分文件未能搜索：" + outcome.stderr[:1000])
    return page.result("ripgrep", partial=bool(page.warnings))


def _glob_match(path, patterns):
    name = path.as_posix()
    for glob in patterns:
        if "/" not in glob and fnmatch.fnmatchcase(path.name, glob):
            return True
        expression, index = "", 0
        while index < len(glob):
            if glob[index:index + 3] == "**/":
                expression += "(?:.*/)?"
                index += 3
            elif glob[index:index + 2] == "**":
                expression += ".*"
                index += 2
            else:
                expression += "[^/]*" if glob[index] == "*" else "[^/]" if glob[index] == "?" else re.escape(glob[index])
                index += 1
        if re.fullmatch(expression, name):
            return True
    return False


def _fallback(args, workspace, root, ignored, listing, mode, pattern, includes, excludes, context, page, deadline):
    if not listing and not args.get("literal", False) and re.search(r"[.\^$*+?{}\[\]\\|()]", pattern):
        return _failure("SEARCH_ENGINE_REQUIRED", "Regex requires ripgrep; install rg, repair GITGO_RIPGREP_PATH, or explicitly use literal=true.", **page.result("python", partial=True))
    if any(any(ch in glob for ch in "{}[\\") for glob in includes + excludes):
        return _failure("SEARCH_ENGINE_REQUIRED", "Advanced globs require ripgrep.", **page.result("python", partial=True))
    boundary = page.boundary
    scanned, read_errors = 0, False

    def files(directory):
        nonlocal scanned, read_errors
        if args.get("respect_ignore", True) and any((directory / n).exists() for n in (".gitignore", ".ignore", ".rgignore")):
            raise ValueError("Ignore files require ripgrep; refusing to silently widen the search.")
        with os.scandir(directory) as entries:
            ordered = []
            for entry in entries:
                scanned += 1
                if time.monotonic() >= deadline or scanned > 100_000 or len(ordered) >= 10000:
                    raise TimeoutError("fallback traversal limit")
                ordered.append(entry)
            ordered.sort(key=lambda e: e.name + ("/" if e.is_dir(follow_symlinks=False) else ""))
        for entry in ordered:
            if time.monotonic() >= deadline:
                raise TimeoutError("fallback traversal limit")
            if entry.is_symlink() or entry.name in ignored or (not args.get("include_hidden", False) and entry.name.startswith(".")):
                continue
            path = Path(entry.path)
            if getattr(path, "is_junction", lambda: False)():
                continue
            if entry.is_dir(follow_symlinks=False):
                if not path.resolve().is_relative_to(boundary):
                    read_errors = True
                    page.warn("SEARCH_SCOPE_SKIPPED", "发现超出搜索范围的目录链接，已跳过。")
                    continue
                if _glob_match(Path(entry.path).relative_to(boundary), excludes):
                    continue
                yield from files(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)

    needle = pattern if args.get("case_sensitive", False) else pattern.lower()
    try:
        candidates = [root] if page.target is not None else files(root)
        for path in candidates:
            checked = _checked_path(str(path), root, page)
            if checked is None:
                read_errors = True
                continue
            path = checked
            relative = path.relative_to(boundary)
            if any(p in ignored for p in relative.parts) or (not args.get("include_hidden", False) and any(p.startswith(".") for p in relative.parts)):
                continue
            if includes and not _glob_match(relative, includes) or _glob_match(relative, excludes):
                continue
            try:
                if listing:
                    if not page.add(_file_row(workspace, path)):
                        break
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                with path.open("rb") as handle:
                    raw = handle.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    continue
                if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                    text = raw.decode("utf-16", errors="replace")
                elif b"\0" in raw:
                    continue
                else:
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeError:
                        text = raw.decode("utf-8-sig", errors="replace")
                        read_errors = True
                        page.warn("SEARCH_DECODE_REPLACED", "部分文本无法按 UTF-8 解码，已替换无效字符；匹配覆盖不完整。")
                lines = text.splitlines()
                count = 0
                for index, line in enumerate(lines):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("fallback search deadline")
                    if needle not in (line if args.get("case_sensitive", False) else line.lower()):
                        continue
                    count += 1
                    if mode == "content":
                        row = {"file": _relative(workspace, path), "line": index + 1, "text": line[:500], "text_truncated": len(line) > 500}
                        if context:
                            row.update(context_before=[{"line": i + 1, "text": lines[i][:500]} for i in range(max(0, index - context), index)],
                                       context_after=[{"line": i + 1, "text": lines[i][:500]} for i in range(index + 1, min(len(lines), index + context + 1))])
                        if not page.add(row):
                            break
                    elif mode == "files":
                        page.add(_file_row(workspace, path))
                        break
                if mode == "count" and count:
                    page.add({"file": _relative(workspace, path), "count": count})
                if page.more:
                    break
            except OSError:
                read_errors = True
                page.warn("SEARCH_FILE_UNAVAILABLE", "部分文件无法读取，结果不完整。")
    except (OSError, ValueError, TimeoutError, RecursionError) as exc:
        page.reason = "fallback_incomplete"
        page.warn("SEARCH_INCOMPLETE", "回退无法完整覆盖搜索范围：" + str(exc)[:300])
        return _failure("SEARCH_ENGINE_REQUIRED" if isinstance(exc, ValueError) else "SEARCH_INCOMPLETE", str(exc)[:300], **page.result("python", partial=True))
    return page.result("python", partial=read_errors)
