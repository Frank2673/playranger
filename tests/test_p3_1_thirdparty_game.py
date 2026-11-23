"""P3-1 第二个真实第三方游戏验证 —— 2048（MIT）。

为什么选 2048：
- 真实第三方开源游戏（gabrielecirulli/2048），源码未做任何修改
- 代码结构与自研 demo_game 完全不同：多文件、原型式
  `GameManager.prototype.setup = function () {}`、`self.xxx` 惯用法、渲染与状态分离
- 能验证定位器不是「只对自己的演示游戏有效」

覆盖：
1. 上游文件完整性（SHA-256 与 PROVENANCE.json 一致，证明未改源码）
2. 原生源码上的定位精度（文件:行:列 + 函数归属）
3. 注入缺陷副本上的三条 patch 策略（遗漏重置 / 渲染偏移 / 渲染未绑定）
4. 无源码时明确降级
5. advisor 注入代码定位
6. CLI `locate` 端到端（JSON / 退出码 / 写文件）

全部为静态单元级测试，不依赖浏览器与网络。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from playranger.core.agent.advisor import advise
from playranger.core.agent.code_locator import locate, locate_bug

REPO_ROOT = Path(__file__).resolve().parents[1]
GAME_ROOT = REPO_ROOT / "tests" / "fixtures" / "thirdparty_2048"
PROVENANCE_PATH = GAME_ROOT / "PROVENANCE.json"


def _load_provenance() -> dict:
    with open(PROVENANCE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find(hits: list, *, path: str, line: int, kind: str | None = None):
    for hit in hits:
        if hit.path == path and hit.line == line and (kind is None or hit.kind == kind):
            return hit
    return None


@pytest.fixture(autouse=True)
def _clear_source_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空源码根环境变量，避免本机环境污染用例。"""
    monkeypatch.delenv("PLAYRANGER_SOURCE_ROOT", raising=False)


@pytest.fixture
def game_copy(tmp_path: Path) -> Path:
    """可修改的第三方源码副本（用于注入缺陷，不污染原生夹具）。"""
    dst = tmp_path / "2048_copy"
    shutil.copytree(GAME_ROOT, dst)
    return dst


def _replace_once(game_root: Path, rel: str, old: str, new: str) -> None:
    path = game_root / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, f"注入点不存在: {rel}: {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# ─── 1. 上游完整性 ─────────────────────────────────────────────


class TestUpstreamIntegrity:
    def test_provenance_declares_upstream_and_mit(self) -> None:
        data = _load_provenance()
        assert data["upstream"] == "https://github.com/gabrielecirulli/2048"
        assert data["license"] == "MIT"
        assert len(data["commit"]) == 40
        license_text = (GAME_ROOT / "LICENSE.txt").read_text(encoding="utf-8")
        assert "The MIT License (MIT)" in license_text
        assert "Gabriele Cirulli" in license_text

    def test_vendored_files_are_unmodified(self) -> None:
        data = _load_provenance()
        for rel, expected in data["files"].items():
            path = GAME_ROOT / rel
            assert path.is_file(), f"缺少上游文件: {rel}"
            assert _sha256(path) == expected, f"上游文件被修改: {rel}"

    def test_no_extra_source_files_in_fixture(self) -> None:
        """夹具目录只允许 PROVENANCE.json 之外的上游文件，防止夹带自研代码。"""
        data = _load_provenance()
        actual = {
            str(path.relative_to(GAME_ROOT)).replace("\\", "/")
            for path in GAME_ROOT.rglob("*")
            if path.is_file()
        }
        assert actual == set(data["files"]) | {"PROVENANCE.json"}

    def test_structure_is_multi_file_prototype_style(self) -> None:
        """证明这是与 demo_game 不同的第二种代码结构。"""
        html = (GAME_ROOT / "index.html").read_text(encoding="utf-8")
        assert '<script src="js/game_manager.js"></script>' in html
        manager = (GAME_ROOT / "js" / "game_manager.js").read_text(encoding="utf-8")
        assert "GameManager.prototype.setup = function () {" in manager
        assert "var self = this;" in manager


# ─── 2. 原生源码定位精度 ────────────────────────────────────────


class TestLocateOnPristineThirdParty:
    def test_score_write_attributed_to_setup(self) -> None:
        hits = locate("score", GAME_ROOT)
        hit = _find(hits, path="js/game_manager.js", line=48, kind="write")
        assert hit is not None, "未命中 setup 里的 this.score = 0"
        assert hit.function == "setup"
        assert hit.column == 5
        assert "this.score" in hit.snippet

    def test_over_write_in_move_is_found(self) -> None:
        hits = locate("over", GAME_ROOT)
        hit = _find(hits, path="js/game_manager.js", line=186, kind="write")
        assert hit is not None
        assert hit.function == "move"
        assert "this.over = true" in hit.snippet

    def test_self_prefix_is_supported(self) -> None:
        """第三方代码常用 var self = this，self.won 也应算状态写点。"""
        hits = locate("won", GAME_ROOT)
        hit = _find(hits, path="js/game_manager.js", line=170, kind="write")
        assert hit is not None
        assert hit.function == "move"
        assert "self.won = true" in hit.snippet

    def test_render_function_attribution_is_not_forEach(self) -> None:
        """回归：forEach(function () {}) 不能被误判成函数名。"""
        hits = locate("score", GAME_ROOT)
        hit = _find(hits, path="js/html_actuator.js", line=110, kind="write")
        assert hit is not None
        assert hit.function == "updateScore"
        assert all(hit.function != "forEach" for hit in hits)

    def test_multi_field_defect_on_real_source(self) -> None:
        bug = {
            "bug_id": "BUG-2048-001",
            "anomaly_type": "score_mismatch",
            "initial_state": {"score": 0, "over": False},
            "final_state": {"score": 2048, "over": True, "won": True},
            "assertion": {"type": "state_equals", "field": "score", "value": 2048},
        }
        result = locate_bug(bug, GAME_ROOT)
        assert result.fields == ["score", "over", "won"]
        assert result.has_hits
        assert set(result.hits) == {"score", "over", "won"}
        assert result.confidence >= 0.6
        assert "源码根" in result.note
        assert result.patch == ""  # 原生源码没有注入缺陷，不应伪造 patch

    def test_degrade_without_source_root(self) -> None:
        bug = {"bug_id": "BUG-2048-002", "final_state": {"score": 10}}
        result = locate_bug(bug)
        assert result.has_hits is False
        assert result.confidence == 0.0
        assert "未提供源码目录" in result.note


# ─── 3. 注入缺陷副本上的 patch 策略 ────────────────────────────


class TestInjectedDefectsOnThirdPartyCopy:
    def test_missing_reset_patch_uses_this_prefix(self, game_copy: Path) -> None:
        # setup 的「新局」和「恢复存档」两个分支都不再写 score
        _replace_once(
            game_copy, "js/game_manager.js", "    this.score       = previousState.score;\n", ""
        )
        _replace_once(game_copy, "js/game_manager.js", "    this.score       = 0;\n", "")

        bug = {
            "bug_id": "BUG-2048-RESET",
            "anomaly_type": "hp_not_reset",  # 语义：字段遗漏重置
            "initial_state": {"score": 40},
            "assertion": {"type": "state_equals", "field": "score", "value": 0},
        }
        result = locate_bug(bug, game_copy)

        assert result.strategies == ["疑似遗漏重置"]
        assert "--- a/js/game_manager.js" in result.patch
        assert "+    this.score = 0;" in result.patch
        assert "state.score" not in result.patch

    def test_render_offset_patch_preserves_this_prefix(self, game_copy: Path) -> None:
        _replace_once(
            game_copy,
            "js/html_actuator.js",
            "this.scoreContainer.textContent = this.score;",
            "this.scoreContainer.textContent = this.score - 100;",
        )
        bug = {
            "bug_id": "BUG-2048-OFFSET",
            "anomaly_type": "score_mismatch",
            "final_state": {"score": 300},
        }
        result = locate_bug(bug, game_copy)

        assert result.strategies == ["渲染偏移"]
        assert "--- a/js/html_actuator.js" in result.patch
        assert "-  this.scoreContainer.textContent = this.score - 100;" in result.patch
        assert "+  this.scoreContainer.textContent = this.score;" in result.patch
        assert "state.score" not in result.patch

    def test_render_literal_patch_preserves_this_prefix(self, game_copy: Path) -> None:
        _replace_once(
            game_copy,
            "js/html_actuator.js",
            "this.scoreContainer.textContent = this.score;",
            'this.scoreContainer.textContent = "0";',
        )
        bug = {
            "bug_id": "BUG-2048-LITERAL",
            "anomaly_type": "ui_display_error",
            "final_state": {"score": 300},
        }
        result = locate_bug(bug, game_copy)

        assert result.strategies == ["渲染未绑定状态"]
        assert "+  this.scoreContainer.textContent = this.score;" in result.patch
        assert "state.score" not in result.patch


# ─── 4. advisor 注入 ───────────────────────────────────────────


class TestAdvisorInjection:
    def test_fix_plan_contains_code_location(self) -> None:
        bug = {
            "bug_id": "BUG-2048-ADV",
            "anomaly_type": "score_mismatch",
            "final_state": {"score": 300},
        }
        plan = advise(bug, source_root=str(GAME_ROOT))
        assert plan.code_location is not None
        assert plan.fix_steps, "修复方案不应为空"
        first = plan.fix_steps[0]
        assert first.file_hint.startswith("js/")
        assert "game_manager.js" in first.file_hint or "html_actuator.js" in first.file_hint
        assert "### 代码定位（静态扫描）" in plan.to_markdown()


# ─── 5. CLI 端到端 ─────────────────────────────────────────────


def _write_defect_bundle(bug_dir: Path) -> None:
    bug_dir.mkdir(parents=True, exist_ok=True)
    (bug_dir / "defect.json").write_text(
        json.dumps(
            {
                "bug_id": "BUG-2048-CLI",
                "title": "分数显示异常",
                "anomaly_type": "score_mismatch",
                "severity": "major",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bug_dir / "initial_state.json").write_text('{"score": 0}', encoding="utf-8")
    (bug_dir / "final_state.json").write_text('{"score": 300}', encoding="utf-8")
    (bug_dir / "assertion.json").write_text(
        '{"type": "state_equals", "field": "score", "value": 300}', encoding="utf-8"
    )


class TestCliEndToEnd:
    def test_cli_locate_json_and_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from playranger.cli import main

        bug_dir = tmp_path / "BUG-2048-CLI"
        _write_defect_bundle(bug_dir)

        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--no-env",
                    "locate",
                    str(bug_dir),
                    "--source",
                    str(GAME_ROOT),
                    "--json",
                    "--write",
                ]
            )

        assert exc.value.code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["fields"] == ["score"]
        assert payload["hits"]["score"]
        assert payload["hits"]["score"][0]["path"].startswith("js/")
        written = bug_dir / "code_location.json"
        assert written.is_file()
        assert json.loads(written.read_text(encoding="utf-8"))["fields"] == ["score"]

    def test_cli_locate_without_source_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from playranger.cli import main

        bug_dir = tmp_path / "BUG-2048-NOSRC"
        _write_defect_bundle(bug_dir)

        with pytest.raises(SystemExit) as exc:
            main(["--no-env", "locate", str(bug_dir), "--json"])

        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["confidence"] == 0.0
        assert "未提供源码目录" in payload["note"]
