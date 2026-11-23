"""P3-1 代码级修复定位测试。

覆盖：字段提取、JS/HTML/Python 静态扫描、行列号、函数归属、patch 草案、
演示游戏的真实定位（遗漏重置 / 渲染偏移）、无源码降级、advisor 与 intel 注入。
全部为单元级测试，不依赖浏览器。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from playranger.core.agent.advisor import advise
from playranger.core.agent.code_locator import (
    CodeLocation,
    build_patch,
    extract_fields,
    extract_fields_from_html,
    locate_bug,
    scan_html,
    scan_js,
    scan_python,
)
from playranger.core.agent.intel import analyze_bug

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_GAME = REPO_ROOT / "examples" / "demo_game"


@pytest.fixture(autouse=True)
def _clear_source_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空源码根环境变量，避免本机环境污染用例。"""
    monkeypatch.delenv("PLAYRANGER_SOURCE_ROOT", raising=False)


# ─── 1. 字段提取 ───────────────────────────────────────────────


class TestExtractFields:
    def test_merges_states_and_filters_meta(self) -> None:
        bug = {
            "initial_state": {
                "hp": 40,
                "logic_step": 3,
                "available_actions": ["click"],
                "scene": "level2",
            },
            "final_state": {"hp": 40, "score": 300, "raw": {"x": 1}, "error": ""},
            "assertion": {"state_equals": {"hp": 100}},
        }
        assert extract_fields(bug) == ["hp", "scene", "score"]

    def test_assertion_flat_and_nested(self) -> None:
        flat = extract_fields(
            {"assertion": {"type": "state_equals", "field": "hp", "value": 100}}
        )
        nested = extract_fields({"assertion": {"state_less_than": {"score": 10}}})
        assert flat == ["hp"]
        assert nested == ["score"]

    def test_max_six_fields(self) -> None:
        bug = {"initial_state": {f"f{i}": i for i in range(9)}}
        assert len(extract_fields(bug)) == 6

    def test_provenance_state_diff(self) -> None:
        fields = extract_fields({}, {"state_diff": [{"field": "hp"}]})
        assert fields == ["hp"]

    def test_extract_fields_from_html(self) -> None:
        html = (
            '<div data-field="hp"></div>'
            "<span data-field='score'></span>"
            '<i data-field="hp"></i>'
        )
        assert extract_fields_from_html(html) == ["hp", "score"]


# ─── 2. JS 扫描 ────────────────────────────────────────────────

JS_SOURCE = """\
function resetLevel() {
  state.hp = state.hp - 10;
  // state.hp = 999;
}
const state = {
  hp: 100,
};
document.getElementById("level2-restart").onclick = () => {
  state.level = 1;
};
"""


class TestScanJs:
    def test_three_kinds(self) -> None:
        hits = scan_js(JS_SOURCE, "game.js", ["hp"])
        kinds = {hit.kind for hit in hits}
        assert kinds == {"write", "read", "declaration"}

    def test_write_line_and_column(self) -> None:
        hits = scan_js(JS_SOURCE, "game.js", ["hp"])
        write = next(hit for hit in hits if hit.kind == "write")
        assert (write.line, write.column) == (2, 3)
        assert write.function == "resetLevel"

    def test_comment_write_ignored(self) -> None:
        hits = scan_js(JS_SOURCE, "game.js", ["hp"])
        assert all(hit.line != 3 for hit in hits)

    def test_reread_on_right_hand_side(self) -> None:
        hits = scan_js(JS_SOURCE, "game.js", ["hp"])
        read = [hit for hit in hits if hit.kind == "read"]
        assert read and read[0].line == 2
        assert read[0].column > 3

    def test_element_id_as_function_name(self) -> None:
        hits = scan_js(JS_SOURCE, "game.js", ["level"])
        write = next(hit for hit in hits if hit.kind == "write")
        assert write.function == "level2-restart"
        assert (write.line, write.column) == (9, 3)


# ─── 3. HTML 扫描 ──────────────────────────────────────────────


class TestScanHtml:
    def test_absolute_line_number(self, tmp_path: Path) -> None:
        html = (
            "<html>\n"
            "<body>\n"
            '<div id="x"></div>\n'
            "<script>\n"
            "  const state = { score: 0 };\n"
            "  function renderUI() {\n"
            '    document.getElementById("t").textContent = state.score;\n'
            "  }\n"
            "</script>\n"
            "</body>\n"
            "</html>\n"
        )
        path = tmp_path / "game.html"
        path.write_text(html, encoding="utf-8")

        hits = scan_html(path, ["score"])
        read = [hit for hit in hits if hit.kind == "read"]
        assert read
        assert read[0].line == 7
        assert read[0].function == "renderUI"

    def test_script_src_block_skipped(self, tmp_path: Path) -> None:
        html = '<html>\n<script src="game.js"></script>\n</html>\n'
        path = tmp_path / "game.html"
        path.write_text(html, encoding="utf-8")
        assert scan_html(path, ["score"]) == []


# ─── 4. Python 扫描 ────────────────────────────────────────────

PY_SOURCE = """\
class Player:
    def __init__(self):
        self.hp = 100

    def show(self):
        print(self.hp)
"""


class TestScanPython:
    def test_write_and_read(self) -> None:
        hits = scan_python(PY_SOURCE, "player.py", ["hp"])
        write = next(hit for hit in hits if hit.kind == "write")
        read = next(hit for hit in hits if hit.kind == "read")
        assert (write.line, write.column, write.function) == (3, 9, "Player.__init__")
        assert (read.line, read.column, read.function) == (6, 15, "Player.show")

    def test_syntax_error_returns_empty(self) -> None:
        assert scan_python("def broken(:", "broken.py", ["hp"]) == []


# ─── 5. patch 草案 ─────────────────────────────────────────────


class TestBuildPatch:
    def test_unified_diff_format(self) -> None:
        patch = build_patch("index.html", 149, "old();", "new1();\nnew2();")
        lines = patch.splitlines()
        assert lines[0] == "--- a/index.html"
        assert lines[1] == "+++ b/index.html"
        assert lines[2] == "@@ -149,1 +149,2 @@"
        assert lines[3] == "-old();"
        assert lines[4] == "+new1();"
        assert lines[5] == "+new2();"
        assert patch.endswith("\n")


# ─── 6. 演示游戏真实定位 ───────────────────────────────────────


class TestDemoGameLocate:
    def test_hp_not_reset_hits_restart_handler(self) -> None:
        bug = {
            "bug_id": "BUG-001",
            "anomaly_type": "hp_not_reset",
            "initial_state": {"scene": "level2", "level": 2, "hp": 100, "score": 300},
        }
        location = locate_bug(bug, source_root=str(DEMO_GAME))

        assert location.has_hits
        assert "疑似遗漏重置" in location.strategies
        assert location.confidence >= 0.6
        assert location.patch.startswith("--- a/index.html")
        # 追加的重置语句与初始值一致
        assert "+      state.hp = 100;" in location.patch
        # patch 落在 level2-restart 处理器内（第 177–184 行之间）
        header = re.search(r"^@@ -(\d+),1", location.patch, re.M)
        assert header and 177 <= int(header.group(1)) <= 184
        # 命中包含 level2-restart 这一函数
        functions = {hit.function for hits in location.hits.values() for hit in hits}
        assert "level2-restart" in functions

    def test_score_mismatch_removes_offset(self) -> None:
        bug = {
            "bug_id": "BUG-002",
            "anomaly_type": "score_mismatch",
            "initial_state": {"score": 300},
        }
        location = locate_bug(bug, source_root=str(DEMO_GAME))

        assert "渲染偏移" in location.strategies
        assert location.patch.startswith("--- a/index.html")
        added = [
            line
            for line in location.patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        assert added
        assert all("- 200" not in line and "-200" not in line for line in added)
        assert any("state.score)" in line for line in added)

    def test_unknown_anomaly_lists_hits_without_patch(self) -> None:
        bug = {"anomaly_type": "logic_error", "initial_state": {"hp": 100}}
        location = locate_bug(bug, source_root=str(DEMO_GAME))
        assert location.has_hits
        assert location.patch == ""
        assert location.strategies == []


class TestRenderLiteralStrategy:
    def test_ui_display_error_binds_literal(self, tmp_path: Path) -> None:
        (tmp_path / "game.js").write_text(
            "function addScore() {\n"
            "  state.score += 10;\n"
            "}\n"
            "function renderHUD() {\n"
            "  document.getElementById('score').textContent = '得分: 0';\n"
            "}\n",
            encoding="utf-8",
        )
        location = locate_bug(
            {"anomaly_type": "ui_display_error", "initial_state": {"score": 300}},
            source_root=str(tmp_path),
        )
        assert "渲染未绑定状态" in location.strategies
        assert "textContent = state.score;" in location.patch


# ─── 7. 降级路径 ───────────────────────────────────────────────

class TestDegrade:
    def test_no_source_root_degrades(self) -> None:
        location = locate_bug(
            {"bug_id": "B-1", "anomaly_type": "hp_not_reset", "initial_state": {"hp": 100}}
        )
        assert isinstance(location, CodeLocation)
        assert location.confidence == 0.0
        assert not location.has_hits
        assert "未提供源码目录" in location.note

    def test_missing_symbol_degrades(self, tmp_path: Path) -> None:
        (tmp_path / "game.js").write_text("const x = 1;\n", encoding="utf-8")
        location = locate_bug(
            {"anomaly_type": "hp_not_reset", "initial_state": {"hp": 100}},
            source_root=str(tmp_path),
        )
        assert location.confidence == 0.0
        assert "未在源码中定位到相关符号" in location.note

    def test_to_dict_shape(self, tmp_path: Path) -> None:
        (tmp_path / "game.js").write_text("state.hp = 100;\n", encoding="utf-8")
        data = locate_bug(
            {"anomaly_type": "hp_not_reset", "initial_state": {"hp": 100}},
            source_root=str(tmp_path),
        ).to_dict()
        assert data["fields"] == ["hp"]
        assert isinstance(data["hits"]["hp"][0], dict)
        assert set(data) == {"fields", "hits", "strategies", "patch", "confidence", "note"}


# ─── 8. advisor / intel 注入 ───────────────────────────────────


class TestAdvisorInjection:
    def test_with_source_root(self) -> None:
        bug = {
            "bug_id": "BUG-001",
            "anomaly_type": "hp_not_reset",
            "initial_state": {"hp": 100},
        }
        plan = advise(bug, source_root=str(DEMO_GAME))

        assert plan.code_location is not None
        assert plan.fix_steps[0].title == "按定位结果修改"
        assert re.fullmatch(r"index\.html:\d+:\d+", plan.fix_steps[0].file_hint)
        assert plan.fix_steps[0].code_hint.startswith("--- a/index.html")
        assert any("not_reproduced" in item for item in plan.verification_steps)
        assert "静态定位" in plan.evidence_note
        assert plan.to_dict()["code_location"]["patch"]
        assert "代码定位（静态扫描）" in plan.to_markdown()

    def test_without_source_root_keeps_rule_plan(self) -> None:
        bug = {
            "bug_id": "BUG-001",
            "anomaly_type": "hp_not_reset",
            "initial_state": {"hp": 100},
        }
        plan = advise(bug)
        assert plan.code_location is None
        assert plan.fix_steps[0].title == "统一状态重置入口"

    def test_intel_forwards_source_root(self) -> None:
        bug = {
            "bug_id": "BUG-002",
            "anomaly_type": "score_mismatch",
            "initial_state": {"score": 300},
        }
        intel = analyze_bug(bug, source_root=str(DEMO_GAME))
        assert intel["fix_plan"]["code_location"] is not None
        assert intel["fix_plan"]["code_location"]["patch"]
