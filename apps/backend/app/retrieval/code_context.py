"""Live lexical RAG over one generated-app workspace.

The retriever deliberately has no cross-project or persistent index. Every call reads the current
workspace and returns candidate excerpts with hashes; callers must still read a source file before
using it as a write precondition.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import ConflictException
from app.schemas.agent_action import ToolExecutionResult
from app.tools.paths import MAX_FILE_BYTES, SKIP_DIRS, is_text_file, sha256_bytes

MAX_RETRIEVAL_FILES = 400
MAX_RETRIEVAL_BYTES = 2 * 1024 * 1024
MAX_RETRIEVAL_RESULTS = 8
MAX_CHUNK_LINES = 80
CHUNK_OVERLAP_LINES = 20
MAX_CHUNK_CHARS = 6000
MAX_RESULT_CHARS = 16_000
MIN_QUERY_TERM_COVERAGE = 0.5

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[0-9]+|[\u4e00-\u9fff]{2,}")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True, slots=True)
class _Chunk:
    path: str
    start_line: int
    end_line: int
    content: str
    content_hash: str
    terms: Counter[str]


def _terms(value: str) -> list[str]:
    normalized = _CAMEL_BOUNDARY.sub(" ", value).replace("/", " ").replace("\\", " ")
    result: list[str] = []
    for match in _IDENTIFIER.findall(normalized):
        token = match.casefold()
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            result.append(token)
            if len(token) > 2:
                result.extend(token[index : index + 2] for index in range(len(token) - 1))
            continue
        result.extend(part for part in token.split("_") if part)
        if "_" in token:
            result.append(token)
    return result


def _iter_source_files(root: Path, path_filter: str | None):
    base = root.resolve()
    for candidate in sorted(base.rglob("*")):
        if not candidate.is_file() or not is_text_file(candidate):
            continue
        relative_parts = candidate.relative_to(base).parts
        if any(part in SKIP_DIRS for part in relative_parts):
            continue
        relative = candidate.relative_to(base).as_posix()
        if relative.startswith("forgeai/") or (path_filter and path_filter not in relative):
            continue
        try:
            candidate.resolve().relative_to(base)
        except ValueError:
            continue
        yield candidate, relative


def _file_chunks(path: str, raw: bytes) -> list[_Chunk]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    lines = text.splitlines()
    if not lines:
        return []
    content_hash = sha256_bytes(raw)
    chunks: list[_Chunk] = []
    step = MAX_CHUNK_LINES - CHUNK_OVERLAP_LINES
    for offset in range(0, len(lines), step):
        selected = lines[offset : offset + MAX_CHUNK_LINES]
        content = "\n".join(selected)[:MAX_CHUNK_CHARS]
        terms = Counter(_terms(f"{path}\n{content}"))
        if terms:
            chunks.append(
                _Chunk(
                    path=path,
                    start_line=offset + 1,
                    end_line=min(len(lines), offset + len(selected)),
                    content=content,
                    content_hash=content_hash,
                    terms=terms,
                )
            )
        if offset + MAX_CHUNK_LINES >= len(lines):
            break
    return chunks


def retrieve_code_context(
    root: Path,
    *,
    query: str,
    path_filter: str | None = None,
    max_results: int = 5,
    tool_call_id: str = "call_retrieve",
) -> ToolExecutionResult:
    """Retrieve ranked candidate code excerpts from the current workspace only."""

    normalized_query = query.strip()
    if len(normalized_query) < 2:
        raise ConflictException("RAG 查询至少需要 2 个字符")
    if len(normalized_query) > 500:
        raise ConflictException("RAG 查询不能超过 500 个字符")
    normalized_filter = path_filter.replace("\\", "/").strip() if path_filter else None
    if normalized_filter and (
        normalized_filter.startswith("/") or ".." in normalized_filter.split("/")
    ):
        raise ConflictException("RAG 路径过滤条件不合法")
    result_limit = min(MAX_RETRIEVAL_RESULTS, max(1, max_results))
    query_terms = Counter(_terms(normalized_query))
    if not query_terms:
        raise ConflictException("RAG 查询缺少可检索词")

    chunks: list[_Chunk] = []
    scanned_files = 0
    scanned_bytes = 0
    scan_truncated = False
    for candidate, relative in _iter_source_files(root, normalized_filter):
        if scanned_files >= MAX_RETRIEVAL_FILES:
            scan_truncated = True
            break
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            continue
        if scanned_bytes + size > MAX_RETRIEVAL_BYTES:
            scan_truncated = True
            break
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        scanned_files += 1
        scanned_bytes += len(raw)
        chunks.extend(_file_chunks(relative, raw))

    document_frequency: Counter[str] = Counter()
    for chunk in chunks:
        document_frequency.update(set(chunk.terms).intersection(query_terms))
    total_chunks = max(1, len(chunks))
    query_folded = normalized_query.casefold()
    ranked: list[tuple[float, _Chunk]] = []
    for chunk in chunks:
        matched_terms = set(chunk.terms).intersection(query_terms)
        if len(matched_terms) / len(query_terms) < MIN_QUERY_TERM_COVERAGE:
            continue
        score = 0.0
        for term, query_weight in query_terms.items():
            frequency = chunk.terms.get(term, 0)
            if not frequency:
                continue
            inverse_frequency = math.log((total_chunks + 1) / (document_frequency[term] + 1)) + 1
            score += query_weight * (1 + math.log(frequency)) * inverse_frequency
        if query_folded in chunk.content.casefold():
            score += 4.0
        if query_folded in chunk.path.casefold():
            score += 3.0
        if score > 0:
            ranked.append((score, chunk))
    ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].start_line))

    hits: list[dict[str, object]] = []
    result_chars = 0
    result_truncated = scan_truncated
    for score, chunk in ranked:
        if len(hits) >= result_limit:
            result_truncated = True
            break
        remaining = MAX_RESULT_CHARS - result_chars
        if remaining <= 0:
            result_truncated = True
            break
        content = chunk.content[:remaining]
        if len(content) < len(chunk.content):
            result_truncated = True
        hits.append(
            {
                "project_scope": "current_workspace",
                "source_kind": "workspace_code",
                "path": chunk.path,
                "line_range": [chunk.start_line, chunk.end_line],
                "content_hash": chunk.content_hash,
                "score": round(score, 4),
                "retrieval_method": "lexical_tfidf",
                "content": content,
            }
        )
        result_chars += len(content)
    return ToolExecutionResult(
        tool_call_id=tool_call_id,
        name="retrieve_code_context",
        ok=True,
        summary=f"RAG 从当前工作区召回 {len(hits)} 个候选片段",
        data={
            "query": normalized_query,
            "path_filter": normalized_filter,
            "hits": hits,
            "scanned_file_count": scanned_files,
            "scanned_bytes": scanned_bytes,
            "truncated": result_truncated,
        },
        truncated=result_truncated,
    )
