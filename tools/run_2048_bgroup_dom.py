"""B1 组：纯 DOM 可观测性消融实验（零接入，无状态探针）。

和 C 组的唯一区别——证据通道：
- C 组：状态探针（读内部 state）+ DOM + 预置期望值断言；
- B1 组：**只读用户可见 DOM**（.score-container / .game-over / .game-won / 棋子值），
  不读任何内部状态。触发动作、固定种子、预置棋盘这些「确定性实验夹具」保持一致，
  以便单独隔离「证据通道」这一个变量。

方法学要点（避免假阳性）：
1. 对每个缺陷，用同一预置/种子/动作，分别跑「干净源码」和「变异源码」，得到两份
   纯 DOM 观测；只有当变异观测在预测方向上偏离干净基线时，才记为「DOM 可检出」。
   这天然排除了「干净版本身也会触发同样现象」的误报。
2. 额外记录 self_evident（无任何先验期望，仅凭 DOM 常识就能判断是 Bug，例如负分），
   用来诚实区分「带期望值的 DOM 断言」与「真正零 oracle 盲测」。

这不是 B2 真盲测（真盲测无期望值、由 LLM/VLM 自主探索），B1 量化的是
「零接入条件下，DOM 这一个通道的捕获上限」。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EDGE = __import__("os").environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
FIXTURE = ROOT / "tests" / "fixtures" / "thirdparty_2048"
DEFAULT_OUT = ROOT / "output" / "experiments" / "2048"


# 复用 C 组的全部确定性夹具（注入/预置/种子垫片/服务/动作定义）
def _load_cgroup():
    spec = importlib.util.spec_from_file_location(
        "cgroup", ROOT / "tools" / "run_2048_cgroup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CG = _load_cgroup()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _start_server(root: Path):
    handler = partial(_QuietHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/index.html"


def _dom_init_script(seed: int, preset: dict) -> str:
    """与 C 组相同的确定性预置 + 种子垫片，但不注入任何读状态探针。"""
    return f"""
(() => {{
  const preset = {json.dumps(preset)};
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
}})();
"""


def _read_dom(page) -> dict:
    """只读用户可见 DOM，不触碰 localStorage / window 内部状态。"""
    return page.evaluate(
        """() => {
          const scoreEl = document.querySelector('.score-container');
          const bestEl = document.querySelector('.best-container');
          const tiles = Array.from(document.querySelectorAll('.tile .tile-inner'))
            .map(e => parseInt(e.textContent.trim(), 10));
          const msg = document.querySelector('.game-message');
          return {
            dom_score: scoreEl ? parseInt(scoreEl.firstChild.textContent.trim(), 10) : null,
            dom_best: bestEl ? parseInt(bestEl.textContent.trim(), 10) : null,
            game_over: !!document.querySelector('.game-over'),
            game_won: !!document.querySelector('.game-won'),
            message_visible: msg ? msg.classList.contains('game-over') || msg.classList.contains('game-won') : false,
            tile_count: tiles.length,
            tile_values: tiles,
          };
        }"""
    )


def _run_dom(browser, source: Path, mutation: dict) -> dict:
    """在给定源码目录上跑触发动作，返回纯 DOM 观测。"""
    test_type = mutation["test_type"]
    preset = {
        "merge_score": CG._preset_merge_2(),
        "restart_score": CG._preset_merge_2(),
        "dom_state_mismatch": CG._preset_merge_2(),
        "no_victory": CG._preset_2048(),
        "early_gameover": CG._preset_full_with_merge(),
    }[test_type]
    actions = ["ArrowLeft", "KeyR"] if test_type == "restart_score" else ["ArrowLeft"]
    wait_ms = 600 if test_type in ("restart_score", "no_victory") else 500

    httpd, url = _start_server(source)
    try:
        ctx = browser.new_context(viewport={"width": 1000, "height": 800})
        ctx.add_init_script(_dom_init_script(mutation["seed"], preset))
        page = ctx.new_page()
        page.goto(url, wait_until="load")
        page.wait_for_timeout(300)
        page.click("body")
        page.wait_for_timeout(200)
        for act in actions:
            page.keyboard.press(act)
            page.wait_for_timeout(wait_ms)
        obs = _read_dom(page)
        ctx.close()
    finally:
        httpd.shutdown()
    return obs


def _detect(test_type: str, clean: dict, mutant: dict) -> tuple[bool, str, bool]:
    """对照干净基线判定。返回 (检出, 证据, 是否无 oracle 自明)。"""
    if test_type == "restart_score":
        # 干净重开 DOM 分=0；变异保留上局分(>0)
        found = clean["dom_score"] == 0 and mutant["dom_score"] is not None and mutant["dom_score"] > 0
        return found, f"干净 DOM 分={clean['dom_score']}，变异={mutant['dom_score']}", False

    if test_type == "merge_score":
        # 干净 2+2 合并 DOM 分=4；变异=2
        found = mutant["dom_score"] != clean["dom_score"] and mutant["dom_score"] < clean["dom_score"]
        return found, f"干净 DOM 分={clean['dom_score']}，变异={mutant['dom_score']}", False

    if test_type == "dom_state_mismatch":
        # 变异把 DOM 分写成 真实分-100，出现负数，无 oracle 也能看出异常
        negative = mutant["dom_score"] is not None and mutant["dom_score"] < 0
        found = mutant["dom_score"] != clean["dom_score"]
        return found, f"干净 DOM 分={clean['dom_score']}，变异={mutant['dom_score']}", bool(negative)

    if test_type == "no_victory":
        # 干净出现 You win；变异不出现
        found = (clean["game_won"] is True) and (mutant["game_won"] is False)
        return found, f"干净 You win={clean['game_won']}，变异={mutant['game_won']}", False

    if test_type == "early_gameover":
        # 干净（满盘但有可合并对）不 game over；变异误判 game over
        found = (clean["game_over"] is False) and (mutant["game_over"] is True)
        return found, f"干净 game-over={clean['game_over']}，变异={mutant['game_over']}", False

    return False, "未知类型", False


def main() -> int:
    out_dir = DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    records = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=EDGE)
        try:
            for m in CG.C_MUTATIONS:
                print(f"  B1 {m['id']} — {m['title']} ...", end=" ", flush=True)
                # 干净源码（直接用原始 fixture）
                clean = _run_dom(browser, FIXTURE, m)
                # 变异源码（复用 C 组的注入与 builds 目录）
                src, actual_line = CG._inject_mutation(m, out_dir)
                mutant = _run_dom(browser, src, m)
                found, evidence, self_evident = _detect(m["test_type"], clean, mutant)
                print("✅ 检出" if found else "❌ 未检出")
                records.append({
                    "id": m["id"],
                    "orig_id": m["orig_id"],
                    "title": m["title"],
                    "category": m["category"],
                    "severity": m["severity"],
                    "test_type": m["test_type"],
                    "target": {"file": m["file"], "line": actual_line},
                    "clean_dom": clean,
                    "mutant_dom": mutant,
                    "found": found,
                    "self_evident_without_oracle": self_evident,
                    "evidence": evidence,
                })
        finally:
            browser.close()

    total = len(records)
    found_n = sum(1 for r in records if r["found"])
    self_n = sum(1 for r in records if r["self_evident_without_oracle"])

    report = {
        "experiment": "2048-defect-battery",
        "group": "B1-dom-only-ablation",
        "fixture_commit": "478b6ec346e3787f589e4af751378d06ded4cbbc",
        "note": "纯 DOM 观测 + 干净/变异对照；触发用的固定种子与预置棋盘是实验夹具，非产品能力",
        "summary": {
            "total": total,
            "detected_with_expected_value": found_n,
            "detected_rate": round(100.0 * found_n / total, 1),
            "self_evident_without_oracle": self_n,
            "self_evident_rate": round(100.0 * self_n / total, 1),
        },
        "records": records,
    }
    (out_dir / "b1_group_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = _render_md(records, total, found_n, self_n)
    (out_dir / "b1_group_report.md").write_text(md, encoding="utf-8")
    print()
    print(md)
    print(f"💾 {out_dir / 'b1_group_report.json'}")
    print(f"💾 {out_dir / 'b1_group_report.md'}")
    return 0


def _render_md(records, total, found_n, self_n) -> str:
    lines = [
        "# 2048 B1 组：纯 DOM 可观测性消融（零接入）",
        "",
        "> 与 C 组唯一差异：证据通道只用用户可见 DOM，不读内部状态；",
        "> 每个缺陷都与「同种子/同预置/同动作」的干净源码基线对照，避免假阳性。",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 | 含义 |",
        "| --- | --- | --- |",
        f"| 缺陷总数 | {total} | 同 C 组 5 个注入缺陷 |",
        f"| DOM 检出率（带期望值断言） | **{found_n}/{total}（{round(100*found_n/total,1)}%）** | 知道正确值时，DOM 通道能否看出差异 |",
        f"| 零 oracle 自明率 | **{self_n}/{total}（{round(100*self_n/total,1)}%）** | 不带任何期望值、仅凭 DOM 常识就能判定是 Bug |",
        "",
        "## 逐条对照",
        "",
        "| ID | 缺陷 | 干净 DOM | 变异 DOM | 检出(带期望) | 零oracle自明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    def brief(r, key) -> str:
        d = r[key]
        return f"分={d['dom_score']} / win={d['game_won']} / over={d['game_over']} / 子数={d['tile_count']}"

    for r in records:
        lines.append(
            f"| {r['id']} | {r['title']} | {brief(r, 'clean_dom')} | "
            f"{brief(r, 'mutant_dom')} | {'✅' if r['found'] else '❌'} | "
            f"{'✅' if r['self_evident_without_oracle'] else '—'} |"
        )

    lines += [
        "",
        "## 结论（诚实边界）",
        "",
        f"1. **带期望值时 DOM 通道捕获 {found_n}/{total}**：2048 把分数/胜负都反映在 DOM，",
        "   状态类缺陷在「知道正确结果」的前提下，纯 DOM 也能与干净基线区分。",
        f"2. **零 oracle 自明仅 {self_n}/{total}**：真实盲测时 Agent 并不知道「正确分数应是多少」，",
        "   除了出现负数这种违背常识的渲染错误，单看一帧 DOM 无法判定「分少了」「该赢没赢」「不该结束却结束」。",
        "   这正是 C 组状态探针（真值）与 B2 语义盲测（LLM/VLM 推理 + 多次复现）存在的理由。",
        "3. 因此 B1 测的是**通道上限**，不是盲测发现率；后者见 B2（无期望值、自主探索）。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
