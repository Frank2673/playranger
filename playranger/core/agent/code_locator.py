"""代码级修复定位 —— 把「状态字段」映射到「源码 文件:行:列」。

解决的问题：
- advisor 只能给「规则模板 + 证据定制」，告诉不了开发者「改哪一行」
- 缺陷包里只有 JSON 状态，没有源码符号的对应关系
- 定位不到时容易假装定位成功，反而误导修复

做法（纯标准库、零第三方依赖、离线可跑）：
1. 从缺陷包状态与断言里提取「状态字段」（最多 6 个）
2. JS / HTML / Python 三种静态扫描器输出读写点（文件:行:列 + 所在函数）
3. 按异常类型套定位策略（遗漏重置 / 渲染偏移 / 渲染未绑定状态），产出 patch 草案
4. 给出置信度；没有源码或没命中时明确降级为通用修复模板

设计原则：
- 只做「定位 + patch 草案」，不自动改写源码
- 任何异常都不外抛：定位失败等于降级，不能拖垮主流程
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

# ─── 数据结构 ───────────────────────────────────────────────────


@dataclass
class CodeHit:
    """一个源码命中点。"""

    path: str        # 相对源码根目录
    line: int
    column: int
    symbol: str      # 字段名
    kind: str        # read / write / declaration
    function: str    # 所在函数（找不到为 "global"）
    snippet: str     # 原始行
    score: float

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "symbol": self.symbol,
            "kind": self.kind,
            "function": self.function,
            "snippet": self.snippet,
            "score": self.score,
        }


@dataclass
class CodeLocation:
    """一条缺陷的代码定位结果。"""

    fields: list[str] = field(default_factory=list)
    hits: dict[str, list[CodeHit]] = field(default_factory=dict)
    strategies: list[str] = field(default_factory=list)
    patch: str = ""
    confidence: float = 0.0
    note: str = ""

    @property
    def has_hits(self) -> bool:
        """任意字段有命中即视为定位成功。"""
        return any(self.hits.values())

    def to_dict(self) -> dict:
        return {
            "fields": self.fields,
            "hits": {
                name: [hit.to_dict() for hit in hits]
                for name, hits in self.hits.items()
            },
            "strategies": self.strategies,
            "patch": self.patch,
            "confidence": self.confidence,
            "note": self.note,
        }


# ─── 字段提取 ───────────────────────────────────────────────────

# 不参与源码定位的元字段（玩法框架 / 探测器自带字段）
_META_FIELDS = {
    "logic_step",
    "available_actions",
    "interactables",
    "game_over",
    "victory",
    "raw",
    "error",
}

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")

_DATA_FIELD_RE = re.compile(r"""data-field=["']([A-Za-z_]\w*)""")

_MAX_FIELDS = 6


def extract_fields(bug: dict, provenance: Any = None) -> list[str]:
    """从缺陷包里提取状态字段（保序去重，最多 6 个）。

    assertion 支持三种形态：
    - {"state_equals": {...}} / {"state_less_than": {...}} / {"state_greater_than": {...}}
    - 扁平 {"type": "state_equals", "field": "hp", "value": 100}
    """
    fields: list[str] = []

    def _add(name: Any) -> None:
        if not isinstance(name, str):
            return
        if name in _META_FIELDS:
            return
        if not _IDENT_RE.fullmatch(name):
            return
        if name not in fields:
            fields.append(name)

    for key in ("initial_state", "final_state"):
        state = bug.get(key) or {}
        if isinstance(state, dict):
            for name in state:
                _add(name)

    assertion = bug.get("assertion") or {}
    if isinstance(assertion, dict):
        for key in ("state_equals", "state_less_than", "state_greater_than"):
            sub = assertion.get(key)
            if isinstance(sub, dict):
                for name in sub:
                    _add(name)
        _add(assertion.get("field"))

    # provenance 里的字段级差异也补进来（dict 或对象都容错）
    for item in _provenance_diff(provenance):
        if isinstance(item, dict):
            _add(item.get("field"))

    return fields[:_MAX_FIELDS]


def extract_fields_from_html(html: str) -> list[str]:
    """从 DOM 的 data-field 属性里提取状态字段。"""
    fields: list[str] = []
    for match in _DATA_FIELD_RE.finditer(html or ""):
        name = match.group(1)
        if name not in fields:
            fields.append(name)
    return fields[:_MAX_FIELDS]


def _provenance_diff(provenance: Any) -> list:
    """容错取 provenance 的 state_diff。"""
    if provenance is None:
        return []
    if isinstance(provenance, dict):
        diff = provenance.get("state_diff")
    else:
        diff = getattr(provenance, "state_diff", None)
    return list(diff) if isinstance(diff, list) else []


# ─── JS 扫描 ────────────────────────────────────────────────────

_JS_FUNC_RES = [
    # function resetLevel(
    re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\("),
    # GameManager.prototype.setup = function (   /   var restart = function (
    re.compile(
        r"(?:[A-Za-z_$][\w$]*\.)*([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\s*\("
    ),
    # document.getElementById("level2-restart").onclick = () => {
    re.compile(
        r"getElementById\s*\(\s*[\"']([^\"']+)[\"']\s*\)\s*\.\s*\w+\s*=\s*"
        r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    ),
    # const foo = () => {
    re.compile(r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"),
    # resetLevel() {   （排除 grid.forEach(function () { 这类回调）
    re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"),
]

# 这些不是函数名：控制流关键字 + 事件属性名（事件属性由 getElementById 规则接管）
_JS_NOT_FUNC = {
    "if", "for", "while", "switch", "catch", "function", "return", "else",
    "do", "with", "typeof", "new", "delete", "await", "yield", "case",
    "onclick", "onchange", "oninput", "onsubmit", "onkeydown", "onkeyup",
    "onload", "addEventListener",
}

# 常见状态容器前缀：state / game / this / self（var self = this 惯用法）/ s
_STATE_PREFIXES = r"state|game|this|self|s"

_ANY_STATE_WRITE_RE = re.compile(
    rf"\b({_STATE_PREFIXES})\.([A-Za-z_]\w*)\s*(?:[+\-*/]?=)(?!=)"
)


def _mask_comments(source: str) -> str:
    """等长屏蔽注释（保留换行），避免注释里的写点被当成真实代码。"""
    chars = list(source)
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < n:
                c = source[i]
                if c == "\\":
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            end = source.find("\n", i)
            if end == -1:
                end = n
            for k in range(i, end):
                chars[k] = " "
            i = end
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for k in range(i, end):
                if chars[k] != "\n":
                    chars[k] = " "
            i = end
            continue
        i += 1
    return "".join(chars)


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_col(starts: list[int], offset: int) -> tuple[int, int]:
    """offset -> (行号, 列号)，均为 1 基。"""
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1, offset - starts[lo] + 1


def _function_starts(source: str) -> list[tuple[int, str]]:
    """枚举所有函数起点（偏移量升序）。"""
    starts: list[tuple[int, str]] = []
    for pattern in _JS_FUNC_RES:
        for match in pattern.finditer(source):
            name = match.group(1)
            if name in _JS_NOT_FUNC:
                continue
            starts.append((match.start(), name))
    starts.sort(key=lambda item: item[0])
    return starts


def _function_at(
    source: str,
    offset: int,
    starts: list[tuple[int, str]] | None = None,
) -> str:
    """取匹配位置之前最近的函数名，找不到为 global。"""
    if starts is None:
        starts = _function_starts(source)
    name = "global"
    for start, func_name in starts:
        if start > offset:
            break
        name = func_name
    return name


def scan_js(source: str, path: str, fields: list[str]) -> list[CodeHit]:
    """轻量静态扫描 JS：写点、对象字面量声明、读点。"""
    if not source:
        return []

    masked = _mask_comments(source)
    starts = _line_starts(source)
    func_starts = _function_starts(source)
    lines = source.splitlines()

    hits: list[CodeHit] = []

    def _emit(offset: int, name: str, kind: str, score: float) -> None:
        line, column = _line_col(starts, offset)
        hits.append(
            CodeHit(
                path=path,
                line=line,
                column=column,
                symbol=name,
                kind=kind,
                function=_function_at(source, offset, func_starts),
                snippet=lines[line - 1].strip() if 0 < line <= len(lines) else "",
                score=score,
            )
        )

    for name in fields:
        escaped = re.escape(name)
        write_re = re.compile(
            rf"\b(?:{_STATE_PREFIXES})\.{escaped}\s*(?:[+\-*/]?=)(?!=)"
        )
        decl_re = re.compile(rf"\b{escaped}\s*:")
        read_re = re.compile(rf"\b(?:{_STATE_PREFIXES})\.{escaped}\b")

        write_spans: list[tuple[int, int]] = []
        for match in write_re.finditer(masked):
            write_spans.append((match.start(), match.end()))
            _emit(match.start(), name, "write", 1.0)

        for match in decl_re.finditer(masked):
            _emit(match.start(), name, "declaration", 0.8)

        for match in read_re.finditer(masked):
            # 写点本身不重复计读
            if any(start <= match.start() < end for start, end in write_spans):
                continue
            _emit(match.start(), name, "read", 0.5)

    return _dedup_hits(hits)


def _dedup_hits(hits: list[CodeHit]) -> list[CodeHit]:
    """按 (path, line, column) 去重，保留分数最高的那条，再按分数降序。"""
    best: dict[tuple[str, int, int], CodeHit] = {}
    for hit in hits:
        key = (hit.path, hit.line, hit.column)
        current = best.get(key)
        if current is None or hit.score > current.score:
            best[key] = hit
    return sorted(
        best.values(),
        key=lambda h: (-h.score, h.path, h.line, h.column),
    )


# ─── HTML 扫描 ──────────────────────────────────────────────────

_SCRIPT_BLOCK_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_SCRIPT_SRC_RE = re.compile(r"\bsrc\s*=", re.I)


def scan_html(path: str | Path, fields: list[str]) -> list[CodeHit]:
    """抽取所有内联 <script> 块，把块内行号换算成文件绝对行号。"""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    hits: list[CodeHit] = []
    for match in _SCRIPT_BLOCK_RE.finditer(text):
        if _SCRIPT_SRC_RE.search(match.group(1) or ""):
            continue
        body = match.group(2) or ""
        # 块内容起始行（<script> 所在行）
        start_line = text.count("\n", 0, match.start(2)) + 1
        for hit in scan_js(body, str(path), fields):
            hit.line += start_line - 1
            hits.append(hit)

    return _dedup_hits(hits)


# ─── Python 扫描 ────────────────────────────────────────────────


class _PyVisitor(ast.NodeVisitor):
    """遍历 AST，标记字段读写点并记录所在类 / 函数。"""

    def __init__(self, path: str, fields: list[str], lines: list[str] | None = None) -> None:
        self.path = path
        self.fields = set(fields)
        self.lines = lines or []
        self.scope: list[tuple[str, str]] = []
        self.writes: set[int] = set()
        self.hits: list[CodeHit] = []

    # ── 作用域 ──
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, "function")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, "class")

    def _visit_scope(self, node: Any, kind: str) -> None:
        self.scope.append((kind, node.name))
        self.generic_visit(node)
        self.scope.pop()

    def _scope_name(self) -> str:
        if not self.scope:
            return "global"
        classes = [name for kind, name in self.scope if kind == "class"]
        funcs = [name for kind, name in self.scope if kind == "function"]
        if funcs:
            prefix = classes[-1] + "." if classes else ""
            return prefix + funcs[-1]
        return classes[-1] if classes else "global"

    # ── 写点目标 ──
    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._mark_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._mark_target(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._mark_target(node.target)
        self.generic_visit(node)

    def _mark_target(self, target: ast.AST) -> None:
        if isinstance(target, (ast.Attribute, ast.Subscript)):
            self.writes.add(id(target))

    # ── 命中 ──
    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._record(node, node.attr)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        name = _const_str(node.slice)
        if name:
            self._record(node, name)
        self.generic_visit(node)

    def _record(self, node: ast.AST, name: str) -> None:
        if name not in self.fields:
            return
        kind = "write" if id(node) in self.writes else "read"
        snippet = ""
        if 0 < node.lineno <= len(self.lines):
            snippet = self.lines[node.lineno - 1].strip()
        self.hits.append(
            CodeHit(
                path=self.path,
                line=node.lineno,
                column=node.col_offset + 1,
                symbol=name,
                kind=kind,
                function=self._scope_name(),
                snippet=snippet,
                score=1.0 if kind == "write" else 0.5,
            )
        )


def _const_str(node: Any) -> str:
    """取常量字符串下标（py3.8+ 为 ast.Constant）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def scan_python(source: str, path: str, fields: list[str]) -> list[CodeHit]:
    """用标准库 ast 扫描 Python 读写点。"""
    if not source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    visitor = _PyVisitor(path, fields, source.splitlines())
    visitor.visit(tree)
    return _dedup_hits(visitor.hits)


# ─── 文件遍历与定位 ─────────────────────────────────────────────

_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__"}
_SCAN_EXTS = {".js", ".ts", ".jsx", ".tsx", ".mjs", ".html", ".py"}
_MAX_FILES = 400
_MAX_FILE_SIZE = 1_500_000


def _iter_sources(roots: Any) -> list[tuple[Path, Path]]:
    """把 roots（文件/目录/列表）展开为 [(文件路径, 源码根)]。"""
    if isinstance(roots, (str, Path)):
        candidates = [roots]
    else:
        candidates = list(roots or [])

    result: list[tuple[Path, Path]] = []
    for item in candidates:
        root = Path(item)
        if root.is_file():
            result.append((root, root.parent))
            if len(result) >= _MAX_FILES:
                return result
            continue
        if not root.is_dir():
            continue
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file():
                continue
            try:
                rel_parts = file_path.relative_to(root).parts
            except ValueError:
                rel_parts = file_path.parts
            if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
                continue
            if file_path.suffix.lower() not in _SCAN_EXTS:
                continue
            try:
                if file_path.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            result.append((file_path, root))
            if len(result) >= _MAX_FILES:
                return result
    return result


def locate(field: str, roots: Any) -> list[CodeHit]:
    """在一个或多个源码根目录里定位字段的读写点（按分数降序去重）。"""
    hits: list[CodeHit] = []
    for file_path, root in _iter_sources(roots):
        try:
            rel = str(file_path.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel = file_path.name
        suffix = file_path.suffix.lower()
        if suffix == ".html":
            file_hits = scan_html(file_path, [field])
            for hit in file_hits:
                hit.path = rel
        elif suffix == ".py":
            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            file_hits = scan_python(source, rel, [field])
        else:
            try:
                source = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            file_hits = scan_js(source, rel, [field])
        hits.extend(file_hits)
    return _dedup_hits(hits)


# ─── patch 草案 ─────────────────────────────────────────────────


def build_patch(path: str, line: int, old_line: str, new_line: str) -> str:
    """生成 unified diff 片段（单行 hunk，new_line 可多行）。"""
    added = str(new_line).splitlines() or [""]
    out = [
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -{line},1 +{line},{len(added)} @@",
        f"-{old_line}",
    ]
    out.extend(f"+{text}" for text in added)
    return "\n".join(out) + "\n"


# ─── 定位策略 ───────────────────────────────────────────────────

STRATEGY_RULES: dict[str, dict[str, str]] = {
    "hp_not_reset": {
        "strategy": "疑似遗漏重置",
        "kind": "reset",
    },
    "score_mismatch": {
        "strategy": "渲染偏移",
        "kind": "render",
    },
    "ui_display_error": {
        "strategy": "渲染未绑定状态",
        "kind": "render",
    },
}

_RESET_FN_RE = re.compile(
    r"reset|restart|init|setup|start|level|scene|newGame|begin", re.I
)
_RENDER_FN_RE = re.compile(r"render|update|show|display|result|win|hud|draw", re.I)
_RENDER_MARK_RE = re.compile(r"textContent|innerHTML|innerText")

# 重置类函数名的优先级：restart/reset 类优先于 init/setup 类，level/start 最低
_RESET_NAME_WEIGHTS = (
    (re.compile(r"reset|restart|newgame", re.I), 3),
    (re.compile(r"init|setup|begin", re.I), 2),
    (re.compile(r"level|scene|start", re.I), 1),
)


def _reset_name_weight(name: str) -> int:
    weight = 0
    for pattern, value in _RESET_NAME_WEIGHTS:
        if pattern.search(name):
            weight = max(weight, value)
    return weight


def _read_text(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _receiver_prefix(text: str, field: str, default: str = "state") -> str:
    """按字段在源码中的出现次数推断接收者前缀（state / this / self ...）。

    第三方代码常写 `this.score` 或 `self.score`，patch 不能硬编码成 `state.score`。
    """
    masked = _mask_comments(text)
    counter: dict[str, int] = {}
    pattern = re.compile(rf"\b({_STATE_PREFIXES})\.{re.escape(field)}\b")
    for match in pattern.finditer(masked):
        prefix = match.group(1)
        counter[prefix] = counter.get(prefix, 0) + 1
    if not counter:
        return default
    return max(counter, key=counter.get)


def _dominant_prefix(sites: list[dict], text: str, field: str) -> str:
    """优先用同函数内兄弟字段的接收者前缀，退回整文件统计。"""
    counter: dict[str, int] = {}
    for site in sites:
        prefix = site.get("prefix")
        if prefix:
            counter[prefix] = counter.get(prefix, 0) + 1
    if counter:
        return max(counter, key=counter.get)
    return _receiver_prefix(text, field)


def _state_write_sites(text: str) -> list[dict]:
    """扫描全文里所有 state.<字段> 写点（用于判断「函数里写了哪些字段」）。"""
    masked = _mask_comments(text)
    starts = _line_starts(text)
    func_starts = _function_starts(text)
    lines = text.splitlines()
    sites: list[dict] = []
    for match in _ANY_STATE_WRITE_RE.finditer(masked):
        line, column = _line_col(starts, match.start())
        sites.append(
            {
                "prefix": match.group(1),
                "field": match.group(2),
                "line": line,
                "column": column,
                "function": _function_at(text, match.start(), func_starts),
                "snippet": lines[line - 1] if 0 < line <= len(lines) else "",
            }
        )
    return sites


def _hit_paths(hits: dict[str, list[CodeHit]]) -> list[str]:
    """按命中数排序的文件列表（命中多的文件优先）。"""
    counter: dict[str, int] = {}
    for items in hits.values():
        for hit in items:
            counter[hit.path] = counter.get(hit.path, 0) + 1
    return sorted(counter, key=lambda p: (-counter[p], p))


def _expected_value(field: str, bug: dict) -> Any:
    """重置值优先取断言的期望值（如 hp=100），否则退回初始状态。"""
    assertion = bug.get("assertion") or {}
    if isinstance(assertion, dict):
        if assertion.get("field") == field and "value" in assertion:
            return assertion["value"]
        equals = assertion.get("state_equals")
        if isinstance(equals, dict) and field in equals:
            return equals[field]
    initial = bug.get("initial_state") or {}
    if isinstance(initial, dict):
        return initial.get(field, 0)
    return 0


def _js_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    return json.dumps(str(value), ensure_ascii=False)


def _missing_reset_patch(
    field: str,
    hits: dict[str, list[CodeHit]],
    bug: dict,
    root: Path,
) -> tuple[str, str]:
    """hp_not_reset：在重置类函数内补一行重置。"""
    value = _expected_value(field, bug)

    best: tuple[int, int, str, str, dict] | None = None
    for rel in _hit_paths(hits):
        text = _read_text(root, rel)
        if not text:
            continue
        by_function: dict[str, list[dict]] = {}
        for site in _state_write_sites(text):
            by_function.setdefault(site["function"], []).append(site)
        for func_name, sites in by_function.items():
            if not _RESET_FN_RE.search(func_name):
                continue
            if any(site["field"] == field for site in sites):
                continue  # 该函数已重置本字段
            others = {site["field"] for site in sites}
            has_read = any(
                hit.function == func_name and hit.kind == "read"
                for hit in hits.get(field, [])
            )
            if not others and not has_read:
                continue
            candidate = (_reset_name_weight(func_name), len(sites), func_name, rel, sites)
            if best is None or candidate[:2] > best[:2]:
                best = candidate

    if best is None:
        return "", ""

    _weight, _count, func_name, rel, sites = best
    text = _read_text(root, rel)
    lines = text.splitlines()
    last = max(sites, key=lambda site: site["line"])
    line = last["line"]
    old_line = lines[line - 1] if 0 < line <= len(lines) else last["snippet"]
    indent = old_line[: len(old_line) - len(old_line.lstrip())]
    prefix = _dominant_prefix(sites, text, field)
    new_line = f"{old_line}\n{indent}{prefix}.{field} = {_js_literal(value)};"
    return build_patch(rel, line, old_line, new_line), func_name


def _render_offset_patch(
    field: str,
    hits: dict[str, list[CodeHit]],
    root: Path,
) -> tuple[str, str]:
    """score_mismatch：去掉渲染表达式里的数值偏移。"""
    offset_re = re.compile(
        rf"((?:{_STATE_PREFIXES})\.){re.escape(field)}\s*[-+]\s*\d+(?:\.\d+)?"
    )
    for rel in _hit_paths(hits):
        text = _read_text(root, rel)
        if not text:
            continue
        lines = text.splitlines()
        for hit in hits.get(field, []):
            if hit.path != rel:
                continue
            if not _RENDER_FN_RE.search(hit.function):
                continue
            if not (0 < hit.line <= len(lines)):
                continue
            old_line = lines[hit.line - 1]
            if not _RENDER_MARK_RE.search(old_line):
                continue
            match = offset_re.search(old_line)
            if not match:
                continue
            new_line = (
                old_line[: match.start()]
                + f"{match.group(1)}{field}"
                + old_line[match.end():]
            )
            return build_patch(rel, hit.line, old_line, new_line), hit.function
    return "", ""


def _first_string_literal(line: str) -> tuple[int, int] | None:
    """找一行里第一个字符串字面量的 (start, end)。"""
    for i, ch in enumerate(line):
        if ch not in ("'", '"'):
            continue
        j = i + 1
        while j < len(line):
            if line[j] == "\\":
                j += 2
                continue
            if line[j] == ch:
                return i, j + 1
            j += 1
        return None
    return None


def _render_literal_patch(
    field: str,
    hits: dict[str, list[CodeHit]],
    root: Path,
) -> tuple[str, str]:
    """ui_display_error：渲染行没引用字段时，把字面量替换成 state.<field>。"""
    for rel in _hit_paths(hits):
        text = _read_text(root, rel)
        if not text:
            continue
        lines = text.splitlines()
        starts = _line_starts(text)
        func_starts = _function_starts(text)
        field_re = re.compile(rf"(?:state|game|this|s)\.{re.escape(field)}\b")
        for index, old_line in enumerate(lines, 1):
            if not _RENDER_MARK_RE.search(old_line):
                continue
            func_name = _function_at(text, starts[index - 1], func_starts)
            if not _RENDER_FN_RE.search(func_name):
                continue
            if field_re.search(old_line):
                continue
            # 只替换赋值号右侧的字面量，避免误改选择器字符串
            eq = old_line.find("=")
            base = eq + 1 if eq != -1 else 0
            literal = _first_string_literal(old_line[base:])
            if literal is None:
                continue
            start, end = literal[0] + base, literal[1] + base
            prefix = _receiver_prefix(text, field)
            new_line = old_line[:start] + f"{prefix}.{field}" + old_line[end:]
            return build_patch(rel, index, old_line, new_line), func_name
    return "", ""


# ─── 源码根解析 ─────────────────────────────────────────────────


def _resolve_source_root(bug: dict, source_root: str | Path | None = None) -> Path | None:
    if source_root:
        return Path(source_root)

    from_bug = bug.get("source_root")
    if from_bug:
        return Path(str(from_bug))

    env_root = os.environ.get("PLAYRANGER_SOURCE_ROOT")
    if env_root:
        return Path(env_root)

    game_url = str(bug.get("game_url") or "")
    if game_url.startswith("file://"):
        parsed = urlparse(game_url)
        raw = unquote(parsed.path or "")
        # Windows 盘符：/E:/game/index.html -> E:/game/index.html
        raw = re.sub(r"^/([A-Za-z]:)", r"\1", raw)
        if raw:
            return Path(raw).parent
    return None


# ─── 生成入口 ───────────────────────────────────────────────────


def locate_bug(
    bug: dict,
    source_root: str | Path | None = None,
    provenance: Any = None,
) -> CodeLocation:
    """为一条缺陷定位源码位置，产出 patch 草案与置信度。

    任何异常都不外抛：定位失败即降级为通用修复模板。
    """
    try:
        return _locate_bug(bug, source_root, provenance)
    except Exception as exc:  # 定位不能拖垮主流程
        return CodeLocation(
            note=f"代码定位失败，已降级为通用修复模板: {exc}",
            confidence=0.0,
        )


def _locate_bug(
    bug: dict,
    source_root: str | Path | None,
    provenance: Any,
) -> CodeLocation:
    fields = extract_fields(bug, provenance)
    if not fields:
        return CodeLocation(
            note="未从缺陷中提取到状态字段，已降级为通用修复模板",
            confidence=0.0,
        )

    root = _resolve_source_root(bug, source_root)
    if root is None:
        return CodeLocation(
            fields=fields,
            note="未提供源码目录，已降级为通用修复模板",
            confidence=0.0,
        )

    hits: dict[str, list[CodeHit]] = {}
    for name in fields:
        file_hits = locate(name, root)
        if file_hits:
            hits[name] = file_hits

    if not hits:
        return CodeLocation(
            fields=fields,
            note="未在源码中定位到相关符号，已降级为通用修复模板",
            confidence=0.0,
        )

    anomaly = str(bug.get("anomaly_type", "")) or "unknown"
    strategies: list[str] = []
    patch = ""

    if anomaly == "hp_not_reset":
        field = _pick_field(fields, hits, ("hp",))
        patch, _func_name = _missing_reset_patch(field, hits, bug, root)
        if patch:
            strategies.append(STRATEGY_RULES["hp_not_reset"]["strategy"])
    elif anomaly == "score_mismatch":
        field = _pick_field(fields, hits, ("score",))
        patch, _func_name = _render_offset_patch(field, hits, root)
        if patch:
            strategies.append(STRATEGY_RULES["score_mismatch"]["strategy"])
    elif anomaly == "ui_display_error":
        field = _pick_field(fields, hits, ("hp", "score"))
        patch, _func_name = _render_offset_patch(field, hits, root)
        if not patch:
            patch, _func_name = _render_literal_patch(field, hits, root)
        if patch:
            strategies.append(STRATEGY_RULES["ui_display_error"]["strategy"])

    confidence = _confidence(hits, patch)
    total = sum(len(items) for items in hits.values())
    note = f"源码根 {root}，命中 {total} 处"
    if strategies:
        note += f"，策略：{'、'.join(strategies)}"

    return CodeLocation(
        fields=fields,
        hits=hits,
        strategies=strategies,
        patch=patch,
        confidence=confidence,
        note=note,
    )


def _pick_field(fields: list[str], hits: dict[str, list[CodeHit]], prefer: tuple[str, ...]) -> str:
    """优先挑业务上最相关的字段（如 score），否则取第一个有命中的字段。"""
    for name in prefer:
        if hits.get(name):
            return name
    for name in fields:
        if hits.get(name):
            return name
    return fields[0] if fields else ""


def _confidence(hits: dict[str, list[CodeHit]], patch: str) -> float:
    score = 0.25
    all_hits = [hit for items in hits.values() for hit in items]
    if any(hit.kind == "write" for hit in all_hits):
        score += 0.2
    if any(
        _RESET_FN_RE.search(hit.function) or _RENDER_FN_RE.search(hit.function)
        for hit in all_hits
    ):
        score += 0.15
    if patch:
        score += 0.2
    if sum(1 for items in hits.values() if items) >= 2:
        score += 0.1
    return round(min(0.9, score), 2)


__all__ = [
    "STRATEGY_RULES",
    "CodeHit",
    "CodeLocation",
    "build_patch",
    "extract_fields",
    "extract_fields_from_html",
    "locate",
    "locate_bug",
    "scan_html",
    "scan_js",
    "scan_python",
]
