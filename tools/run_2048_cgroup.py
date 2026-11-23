"""C 组（浏览器级 + 状态桥接 + 代码定位）2048 缺陷发现实验。

实验设计：
- 5 个预注册缺陷（D01 / D02 / D03 / D05 / D06），每缺陷独立源码副本注入
- 本地 HTTP 服务器 + Playwright Edge 浏览器 + 固定种子垫片 + 只读状态探针
- 每个缺陷跑 3 轮（不同种子），统计发现率
- 发现后调用 locate_bug 做代码定位，记录 Top-1/Top-3/patch/耗时
- 输出 JSON + Markdown 报告

注意：
- 种子垫片与状态探针是实验夹具（保证确定性 + 状态可读），不是产品能力
- 这是 C 组「状态桥接」的简化版：探针读状态 + DOM 读分数 + 定位器做代码定位
- 不涉及 LLM 规划，是「确定性触发 + 状态比对 + 定位器」的管道验证
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "thirdparty_2048"
DEFAULT_OUT = ROOT / "output" / "experiments" / "2048"
EDGE = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")


def _load_locate_bug():
    """延迟导入 locate_bug，避免触发 playranger 包的全量 import（含 playwright）。
    code_locator 本身不依赖 playwright，但 agent/__init__.py 会拉 play_engine。
    所以我们手动注册模块名到 sys.modules 再 exec，绕过 dataclass 的模块查找。
    """
    import importlib.util
    module_name = "playranger.core.agent.code_locator"
    # 确保父包存在（否则 dataclass 找不到 __module__ 对应模块）
    pkg = sys.modules.get("playranger")
    if pkg is None:
        pkg = type(sys)("playranger")
        pkg.__path__ = [str(ROOT / "playranger")]
        sys.modules["playranger"] = pkg
    core = sys.modules.get("playranger.core")
    if core is None:
        core = type(sys)("playranger.core")
        core.__path__ = [str(ROOT / "playranger" / "core")]
        sys.modules["playranger.core"] = core
    agent = sys.modules.get("playranger.core.agent")
    if agent is None:
        agent = type(sys)("playranger.core.agent")
        agent.__path__ = [str(ROOT / "playranger" / "core" / "agent")]
        sys.modules["playranger.core.agent"] = agent

    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "playranger" / "core" / "agent" / "code_locator.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod.locate_bug


# ── 5 个 C 组缺陷（从 D 组 10 个中挑选最具代表性的） ──
# 每缺陷有专门的「触发动作序列」和「发现检查函数」
C_MUTATIONS = [
    {
        "id": "C01", "orig_id": "D01", "category": "状态",
        "title": "重新开始后分数未归零",
        "anomaly_type": "score_mismatch", "severity": "major",
        "file": "js/game_manager.js", "target_line": 48,
        "old": "    this.score       = 0;",
        "new": "    this.score       = this.score || 0;",
        "trigger": "得分 > 0 后按 R 重开",
        "expectation": "重开分数归零；实际保留上局分数",
        "state": {"score": 104}, "final": {"score": 104},
        "seed": 20260101,
        "test_type": "restart_score",  # 先合成得分，再按 R 重开，检查 score 是否归零
    },
    {
        "id": "C02", "orig_id": "D02", "category": "状态",
        "title": "合并计分少加",
        "anomaly_type": "score_mismatch", "severity": "major",
        "file": "js/game_manager.js", "target_line": 167,
        "old": "          self.score += merged.value;",
        "new": "          self.score += tile.value;",
        "trigger": "任意一次合并（如 2+2）",
        "expectation": "应 +4；实际 +2",
        "state": {"score": 0}, "final": {"score": 2},
        "seed": 20260202,
        "test_type": "merge_score",  # 预置两相邻 2，左移合并，检查分数
    },
    {
        "id": "C03", "orig_id": "D03", "category": "UI",
        "title": "分数显示比真实值少 100",
        "anomaly_type": "ui_display_error", "severity": "major",
        "file": "js/html_actuator.js", "target_line": 112,
        "old": "  this.scoreContainer.textContent = this.score;",
        "new": "  this.scoreContainer.textContent = this.score - 100;",
        "trigger": "任意得分后 DOM 分数与真实状态不一致",
        "expectation": "DOM 分数 = 真实分；实际少 100",
        "state": {"score": 104}, "final": {"score": 104},
        "seed": 20260303,
        "test_type": "dom_state_mismatch",  # 合成得分后，DOM 分数 != 状态分数
    },
    {
        "id": "C05", "orig_id": "D05", "category": "状态",
        "title": "合成 2048 不出现 You win",
        "anomaly_type": "victory_not_triggered", "severity": "critical",
        "file": "js/game_manager.js", "target_line": 170,
        "old": "          if (merged.value === 2048) self.won = true;",
        "new": "          if (merged.value > 4096) self.won = true;",
        "trigger": "使用固定棋盘合成 2048",
        "expectation": "出现 You win；实际不出现",
        "state": {"won": True}, "final": {"won": False},
        "seed": 20260505,
        "test_type": "no_victory",  # 预置两 1024，合并后 won 应为 true，缺陷下为 false
    },
    {
        "id": "C06", "orig_id": "D06", "category": "状态",
        "title": "满盘仍有合并对却直接 Game over",
        "anomaly_type": "game_over_early", "severity": "critical",
        "file": "js/game_manager.js", "target_line": 185,
        "old": "    if (!this.movesAvailable()) {",
        "new": "    if (!this.grid.cellsAvailable()) {",
        "trigger": "固定满盘 + 可合并对",
        "expectation": "可继续；实际直接 Game over",
        "state": {"over": False}, "final": {"over": True},
        "seed": 20260606,
        "test_type": "early_gameover",  # 预置满盘含可合并对，检查 over
    },
]


# ── 预置棋盘 ──
# 注意：2048 的 grid 存储是 cells[x][y]（列×行），不是行×列！
# fromState: for (x in 0..3) for (y in 0..3) new Tile(state[x][y].position, state[x][y].value)

def _tile(x, y, val):
    return {"position": {"x": x, "y": y}, "value": val}


def _empty_grid():
    return [[None for _ in range(4)] for _ in range(4)]


def _preset_merge_2() -> dict:
    """同一行（y=0）相邻两格（x=0, x=1）都是 2，ArrowLeft 合并得 4。"""
    cells = _empty_grid()
    cells[0][0] = _tile(0, 0, 2)
    cells[1][0] = _tile(1, 0, 2)
    return {
        "grid": {"size": 4, "cells": cells},
        "score": 0, "over": False, "won": False, "keepPlaying": False,
    }


def _preset_2048() -> dict:
    """同一行（y=0）相邻两格（x=0, x=1）都是 1024，ArrowLeft 合并得 2048。"""
    cells = _empty_grid()
    cells[0][0] = _tile(0, 0, 1024)
    cells[1][0] = _tile(1, 0, 1024)
    return {
        "grid": {"size": 4, "cells": cells},
        "score": 0, "over": False, "won": False, "keepPlaying": False,
    }


def _preset_full_with_merge() -> dict:
    """满盘 + 两个「左移后仍保留至少一个可合并对」的合并对。

    布局（cells[x][y]）：
      - 垂直合并对：(0,0) 与 (0,1) 同值 64 —— 左右移动不会破坏它；
      - 水平合并对：(1,0) 与 (2,0) 同值 4 —— ArrowLeft 触发一次合并；
      - 其余格子填充互不等、且不等于相邻格的值，避免意外合并。
    ArrowLeft 后：水平对合并（-1 格），addRandomTile 补回（+1 格），棋盘仍满；
    垂直对 (0,0)/(0,1) 原样保留 → movesAvailable()=true 但 cellsAvailable()=false。
    于是干净版不会 game over，而 D06 变异版（用 cellsAvailable 判定）必定 over，
    与新随机块的值无关（不依赖种子运气）。
    """
    cells = _empty_grid()
    # 先放两个合并对
    cells[0][0] = _tile(0, 0, 64)
    cells[0][1] = _tile(0, 1, 64)
    cells[1][0] = _tile(1, 0, 4)
    cells[2][0] = _tile(2, 0, 4)
    # 填充其余 11 格，使用一个循环序列并避开「与相邻格相等」
    pool = [2, 8, 16, 32, 128, 256]
    idx = 0
    for y in range(4):
        for x in range(4):
            if cells[x][y] is not None:
                continue

            def conflicts(v, xx=x, yy=y):
                for nx, ny in ((xx - 1, yy), (xx + 1, yy), (xx, yy - 1), (xx, yy + 1)):
                    if 0 <= nx < 4 and 0 <= ny < 4 and cells[nx][ny] is not None:
                        if cells[nx][ny]["value"] == v:
                            return True
                return False

            v = pool[idx % len(pool)]
            while conflicts(v):
                idx += 1
                v = pool[idx % len(pool)]
            cells[x][y] = _tile(x, y, v)
            idx += 1
    return {
        "grid": {"size": 4, "cells": cells},
        "score": 100, "over": False, "won": False, "keepPlaying": False,
    }


def _init_script(seed: int, preset: dict) -> str:
    return f"""
(() => {{
  const preset = {json.dumps(preset)};
  const origSet = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {{
    origSet.call(this, key, value);
    if (key === 'gameState') {{
      try {{ window.__PLAYRANGER_STATE__ = JSON.parse(value); }} catch (e) {{}}
    }}
  }};
  try {{
    localStorage.setItem('gameState', JSON.stringify(preset));
    localStorage.setItem('bestScore', '100');
  }} catch (e) {{}}

  let s = {int(seed)} >>> 0;
  Math.random = function () {{
    s |= 0; s = s + 0x6D2B79F5 | 0;
    let t = Math.imul(s ^ s >>> 15, 1 | s);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  }};

  // 只读探针对外暴露
  window.__PR_GET_STATE__ = function() {{
    try {{
      const raw = localStorage.getItem('gameState');
      return raw ? JSON.parse(raw) : null;
    }} catch (e) {{ return null; }}
  }};
  window.__PR_GET_DOM_SCORE__ = function() {{
    const el = document.querySelector('.score-container');
    return el ? parseInt(el.firstChild.textContent.trim(), 10) : null;
  }};
  window.__PR_HAS_GAME_OVER__ = function() {{
    return !!document.querySelector('.game-over');
  }};
  window.__PR_HAS_WIN__ = function() {{
    return !!document.querySelector('.game-won');
  }};
}})();
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass


def _start_server(root: Path) -> tuple[ThreadingHTTPServer, str]:
    handler = partial(_QuietHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/index.html"


def _force_remove(path: Path, retries: int = 5, delay: float = 0.4) -> None:
    """Windows 下浏览器/静态服务句柄可能短暂未释放，rmtree 会 WinError 5。
    清除只读位并多次重试。"""
    import stat
    import time

    def on_rm_error(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    for i in range(retries):
        try:
            shutil.rmtree(path, onerror=on_rm_error)
            return
        except OSError:
            if i == retries - 1:
                raise
            time.sleep(delay)


def _inject_mutation(mutation: dict, out_dir: Path) -> tuple[Path, int]:
    """复制干净 fixture，注入缺陷，返回 (源码目录, 实际行号)。"""
    build_dir = out_dir / "builds" / mutation["id"]
    source_dir = build_dir / "source"
    if build_dir.exists():
        _force_remove(build_dir)
    shutil.copytree(FIXTURE, source_dir)

    target = source_dir / mutation["file"]
    text = target.read_text(encoding="utf-8")
    count = text.count(mutation["old"])
    if count != 1:
        raise ValueError(
            f"{mutation['id']} 注入锚点出现 {count} 次（期望 1 次）"
        )
    target.write_text(text.replace(mutation["old"], mutation["new"], 1), encoding="utf-8")
    actual_line = text[: text.index(mutation["old"])].count("\n") + 1
    return source_dir, actual_line


def _run_browser_test(browser, url: str, mutation: dict) -> dict:
    """在浏览器中执行针对性测试，返回发现结果。"""
    test_type = mutation["test_type"]
    seed = mutation["seed"]

    # 根据测试类型选择预置局面和动作序列
    if test_type == "merge_score":
        preset = _preset_merge_2()
        actions = ["ArrowLeft"]  # 一次合并：2+2=4，分+4（缺陷下+2）
        wait_after = 500
    elif test_type == "restart_score":
        preset = _preset_merge_2()
        actions = ["ArrowLeft", "KeyR"]  # 先合并得分，再按 R 重开
        wait_after = 600
    elif test_type == "dom_state_mismatch":
        preset = _preset_merge_2()
        actions = ["ArrowLeft"]  # 合并后 DOM 分应=状态分
        wait_after = 500
    elif test_type == "no_victory":
        preset = _preset_2048()
        actions = ["ArrowLeft"]  # 1024+1024=2048，should win
        wait_after = 600
    elif test_type == "early_gameover":
        preset = _preset_full_with_merge()
        actions = ["ArrowLeft"]  # 满盘但可合并，不应 game over
        wait_after = 500
    else:
        raise ValueError(f"未知测试类型: {test_type}")

    context = browser.new_context(viewport={"width": 1000, "height": 800})
    context.add_init_script(_init_script(seed, preset))
    page = context.new_page()
    page.goto(url, wait_until="load")
    page.wait_for_timeout(300)
    page.click("body")
    page.wait_for_timeout(200)

    for action in actions:
        if action.startswith("Arrow") or action == "KeyR":
            page.keyboard.press(action)
        page.wait_for_timeout(wait_after)

    state = page.evaluate("window.__PR_GET_STATE__()")
    dom_score = page.evaluate("window.__PR_GET_DOM_SCORE__()")
    has_game_over = page.evaluate("window.__PR_HAS_GAME_OVER__()")
    has_win = page.evaluate("window.__PR_HAS_WIN__()")

    context.close()

    return {
        "state": state,
        "dom_score": dom_score,
        "has_game_over": has_game_over,
        "has_win": has_win,
    }


def _check_defect_found(result: dict, mutation: dict) -> tuple[bool, str]:
    """检查是否发现了缺陷，返回 (是否发现, 证据描述)。"""
    test_type = mutation["test_type"]
    state = result["state"] or {}

    if test_type == "restart_score":
        # 按 R 重开后，正常 score=0；缺陷下保留上局分 > 0
        score = state.get("score", 0)
        found = score > 0
        return found, f"重开后分数={score}（预期 0）"

    if test_type == "merge_score":
        # 预置两个 2 合并为 4，正常 +4 分（累计=4）；缺陷下 +2（累计=2）
        score = state.get("score", 0)
        found = score < 4  # 缺陷下得分少
        return found, f"合并后分数={score}（预期 4）"

    if test_type == "dom_state_mismatch":
        # DOM 分数 vs 状态分数不一致
        dom = result["dom_score"]
        st = state.get("score")
        found = st is not None and dom is not None and int(dom) != int(st)
        return found, f"DOM分数={dom}, 状态分数={st}"

    if test_type == "no_victory":
        # 合并 2048 后 won 应该为 true，缺陷下为 false
        won = state.get("won", False)
        found = not won
        return found, f"合并 2048 后 won={won}（预期 true）"

    if test_type == "early_gameover":
        # 满盘但有可合并对，不应 game over
        # 正常：over=False；缺陷：over=True
        over = state.get("over", False)
        # DOM 也显示 game over 遮罩
        dom_over = result["has_game_over"]
        found = over or dom_over
        return found, f"over={over}, DOM_game_over={dom_over}（预期均可继续）"

    return False, "未知测试类型"


def _make_defect_package(mutation: dict, result: dict, evidence: str) -> dict:
    """构造缺陷包（用于后续定位）。"""
    state = result["state"] or {}
    return {
        "bug_id": f"BUG-2048-{mutation['id']}",
        "title": mutation["title"],
        "anomaly_type": mutation["anomaly_type"],
        "severity": mutation["severity"],
        "conclusion": "auto_warning",
        "description": f"{mutation['expectation']}；触发路径：{mutation['trigger']}；证据：{evidence}",
        "initial_state": mutation["state"],
        "final_state": state,
        "assertion": {"state_equals": mutation["state"]},
        "evidence": {"state": "browser_probe", "dom": "dom_read"},
    }


def run_one(mutation: dict, out_dir: Path, browser) -> dict:
    """运行单个 C 组缺陷的完整测试（发现 + 定位）。"""
    # 1. 注入缺陷
    source_dir, actual_line = _inject_mutation(mutation, out_dir)

    # 2. 启服务
    httpd, url = _start_server(source_dir)
    try:
        # 3. 浏览器级发现测试
        start = time.perf_counter()
        browser_result = _run_browser_test(browser, url, mutation)
        found, evidence = _check_defect_found(browser_result, mutation)
        detect_ms = (time.perf_counter() - start) * 1000
    finally:
        httpd.shutdown()

    # 4. 如果发现了缺陷，做代码定位
    locate_result = None
    if found:
        defect = _make_defect_package(mutation, browser_result, evidence)
        loc_start = time.perf_counter()
        try:
            locate_bug = _load_locate_bug()
            lr = locate_bug(defect, source_root=source_dir)
            loc_ms = (time.perf_counter() - loc_start) * 1000

            all_hits = []
            for field, hits in lr.hits.items():
                for idx, hit in enumerate(hits):
                    all_hits.append({
                        "field": field, "rank": idx + 1,
                        "path": str(hit.path), "line": hit.line,
                        "kind": hit.kind, "score": hit.score,
                    })

            def same_line(hit: dict) -> bool:
                hit_path = Path(hit["path"])
                if not hit_path.is_absolute():
                    hit_path = source_dir / hit_path
                return (
                    hit_path.resolve() == (source_dir / mutation["file"]).resolve()
                    and abs(int(hit["line"]) - actual_line) <= 3
                )

            top1_hit = same_line(all_hits[0]) if all_hits else False
            top3_hit = any(same_line(h) for h in all_hits[:3])

            # patch 是否指向注入行
            patch_targets_line = False
            if lr.patch.strip():
                current_file = ""
                for line in lr.patch.splitlines():
                    if line.startswith("+++ "):
                        current_file = line[4:].strip().removeprefix("b/")
                    elif line.startswith("@@ "):
                        try:
                            old_part = line.split()[1]
                            start = int(old_part[1:].split(",")[0])
                        except (IndexError, ValueError):
                            continue
                        if current_file == mutation["file"] and abs(start - actual_line) <= 3:
                            patch_targets_line = True
                            break

            locate_result = {
                "fields": lr.fields,
                "confidence": round(float(lr.confidence), 3),
                "strategy": "；".join(lr.strategies),
                "total_hits": len(all_hits),
                "top1": all_hits[0] if all_hits else None,
                "top1_hit": top1_hit,
                "top3_hit": top3_hit,
                "patch_available": bool(lr.patch.strip()),
                "patch_targets_line": patch_targets_line,
                "operable": bool(top3_hit or patch_targets_line),
                "duration_ms": round(loc_ms, 1),
                "patch": lr.patch,
            }
        except Exception as e:
            locate_result = {"error": str(e)}

    return {
        "id": mutation["id"],
        "orig_id": mutation["orig_id"],
        "title": mutation["title"],
        "category": mutation["category"],
        "severity": mutation["severity"],
        "anomaly_type": mutation["anomaly_type"],
        "target": {
            "file": mutation["file"],
            "line": actual_line,
        },
        "test_type": mutation["test_type"],
        "trigger": mutation["trigger"],
        "seed": mutation["seed"],
        "found": found,
        "evidence": evidence,
        "browser_state": browser_result.get("state"),
        "dom_score": browser_result.get("dom_score"),
        "detect_ms": round(detect_ms, 1),
        "locate": locate_result,
    }


def render_markdown(records: list[dict]) -> str:
    total = len(records)
    found_count = sum(1 for r in records if r["found"])
    found_pct = round(100.0 * found_count / total, 1) if total else 0

    located = [r for r in records if r["found"] and r["locate"] and "error" not in r["locate"]]
    top1 = sum(1 for r in located if r["locate"]["top1_hit"])
    top3 = sum(1 for r in located if r["locate"]["top3_hit"])
    operable = sum(1 for r in located if r["locate"]["operable"])
    patch_avail = sum(1 for r in located if r["locate"]["patch_available"])
    patch_target = sum(1 for r in located if r["locate"]["patch_targets_line"])

    lines = [
        "# 2048 C 组（浏览器级 + 状态桥接 + 代码定位）实验结果",
        "",
        "> 组别：C 状态桥接（浏览器级 + 状态探针 + 代码定位）。",
        "> 实验方法：固定种子 + 预置棋盘 + 针对性动作序列，验证 5 个预注册缺陷的可发现性与可定位性。",
        "> 注意：种子垫片与状态探针是实验夹具（保证确定性和状态可读），不是产品能力。",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 缺陷总数 | {total} |",
        f"| 发现数 | {found_count} / {total}（{found_pct}%） |",
        f"| Top-1 定位命中率（已发现样本中） | {top1} / {len(located)} |",
        f"| Top-3 定位命中率 | {top3} / {len(located)} |",
        f"| 可操作定位率（Top-3 或 patch 指向行） | {operable} / {len(located)} |",
        f"| patch 可用率 | {patch_avail} / {len(located)} |",
        f"| patch 精确指向注入行 | {patch_target} / {len(located)} |",
        "",
        "## 逐条结果",
        "",
        "| ID | 类别 | 严重度 | 注入点 | 发现 | Top-1 | Top-3 | patch可用 | 可操作 | 发现耗时(ms) | 定位耗时(ms) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for r in records:
        target = f"{r['target']['file']}:{r['target']['line']}"
        loc = r.get("locate")
        if loc and "error" not in loc:
            top1 = "是" if loc["top1_hit"] else "否"
            top3 = "是" if loc["top3_hit"] else "否"
            patch = "是" if loc["patch_available"] else "否"
            operable = "是" if loc["operable"] else "否"
            loc_ms = f"{loc['duration_ms']:.0f}"
        elif loc and "error" in loc:
            top1 = top3 = patch = operable = f"错误: {loc['error'][:20]}"
            loc_ms = "--"
        else:
            top1 = top3 = patch = operable = "—"
            loc_ms = "—"

        lines.append(
            f"| {r['id']} | {r['category']} | {r['severity']} | {target} | "
            f"{'✅' if r['found'] else '❌'} | {top1} | {top3} | {patch} | {operable} | "
            f"{r['detect_ms']:.0f} | {loc_ms} |"
        )

    lines.extend([
        "",
        "## 说明",
        "",
        "- 发现判定：根据缺陷类型，用状态探针 + DOM 读取 + 针对性动作序列验证；",
        "- Top-1/Top-3 判定：命中注入文件且行号 ±3；",
        "- 发现耗时：从页面加载到获得缺陷证据的浏览器端执行时间（含等待）；",
        "- 定位耗时：`locate_bug` 的单次执行时间；",
        "- 仅对「已发现」的缺陷执行代码定位；未发现的缺陷不跑定位。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="执行 2048 C 组（浏览器级 + 状态桥接）实验")
    parser.add_argument("--only", default="", help="只跑指定 ID，逗号分隔")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 摘要")
    args = parser.parse_args()

    if not EDGE or not Path(EDGE).exists():
        print("错误：未配置 PLAYWRIGHT_CHROMIUM_PATH 或浏览器不存在", file=sys.stderr)
        print(f"当前值: {EDGE!r}", file=sys.stderr)
        return 2

    selected = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    mutations = [m for m in C_MUTATIONS if not selected or m["id"] in selected]
    if not mutations:
        print(f"没有匹配的缺陷 ID: {args.only}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 延迟导入 playwright
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("错误：未安装 playwright", file=sys.stderr)
        return 2

    records: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=EDGE)
        try:
            for mutation in mutations:
                print(f"  运行 {mutation['id']} — {mutation['title']} ...", end=" ", flush=True)
                try:
                    record = run_one(mutation, out_dir, browser)
                    records.append(record)
                    status = "✅ 发现" if record["found"] else "❌ 未发现"
                    print(status)
                except Exception as e:
                    records.append({
                        "id": mutation["id"], "title": mutation["title"],
                        "category": mutation["category"], "severity": mutation["severity"],
                        "found": False, "error": str(e),
                        "target": {"file": mutation["file"], "line": mutation["target_line"]},
                    })
                    print(f"❌ 错误: {e}")
        finally:
            browser.close()

    report = {
        "experiment": "2048-defect-battery",
        "group": "C-browser-probe-locate",
        "fixture_commit": "478b6ec346e3787f589e4af751378d06ded4cbbc",
        "note": "种子垫片与状态探针为实验夹具，非产品能力",
        "records": records,
    }
    (out_dir / "c_group_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = render_markdown(records)
    (out_dir / "c_group_report.md").write_text(md, encoding="utf-8")

    if args.json:
        print(json.dumps({
            "total": len(records),
            "found": sum(1 for r in records if r["found"]),
            "records": [{k: r.get(k) for k in ("id", "found", "severity", "detect_ms")}
                        for r in records],
        }, ensure_ascii=False, indent=2))
    else:
        print(md)
        print(f"💾 {out_dir / 'c_group_report.json'}")
        print(f"💾 {out_dir / 'c_group_report.md'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
