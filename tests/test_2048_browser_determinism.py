"""2048 浏览器级确定性回放与状态探针测试（P0a）。

做了什么：
- 本地静态服务器托管真实 2048 fixture（MIT）
- 注入固定种子垫片（mulberry32 覆写 Math.random）+ 只读状态探针（hook localStorage.setItem）
- 预置固定棋盘（两个 2 相邻），回放一次左移，比较两次独立加载的最终状态
- 对 D03 注入副本（渲染分 -100）验证「探针分数 vs DOM 分数」不一致可被发现

边界：
- 种子垫片与状态探针是实验夹具，不是产品能力
- 只验证「确定性 + 探针可发现渲染不一致」，不等于 C 组全量实验
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "thirdparty_2048"
EDGE = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
pytestmark = pytest.mark.skipif(not EDGE or not Path(EDGE).exists(), reason="未配置本机浏览器")


def _preset_state() -> dict:
    empty = None
    cells = [
        [{"position": {"x": 0, "y": 0}, "value": 2}, empty, empty, empty],
        [{"position": {"x": 1, "y": 0}, "value": 2}, empty, empty, empty],
        [empty, empty, empty, empty],
        [empty, empty, empty, empty],
    ]
    return {"grid": {"size": 4, "cells": cells}, "score": 100,
            "over": False, "won": False, "keepPlaying": False}


def _init_script(seed: int = 20261123) -> str:
    return f"""
(() => {{
  const preset = {json.dumps(_preset_state())};
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
}})();
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("2048-serve")
    shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
    handler = partial(_QuietHandler, directory=str(root))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
    httpd.shutdown()


def _play_once(browser, url: str) -> dict:
    context = browser.new_context(viewport={"width": 1000, "height": 800})
    context.add_init_script(_init_script())
    page = context.new_page()
    page.goto(url, wait_until="load")
    page.wait_for_timeout(200)
    page.click("body")
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(400)
    state = page.evaluate("window.__PLAYRANGER_STATE__")
    dom = page.evaluate(
        "() => document.querySelector('.score-container').firstChild.textContent.trim()"
    )
    context.close()
    return {"state": state, "dom_score": dom}


def test_replay_is_deterministic(server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=EDGE)
        try:
            first = _play_once(browser, server)
            second = _play_once(browser, server)
        finally:
            browser.close()

    assert first["state"] and second["state"]
    assert first["state"]["score"] == 104
    assert first["state"] == second["state"]
    assert first["dom_score"] == second["dom_score"] == "104"


def test_probe_detects_render_mismatch(server, tmp_path):
    def run(url: str) -> dict:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=EDGE)
            try:
                return _play_once(browser, url)
            finally:
                browser.close()

    # 干净版本：探针分数与 DOM 一致
    clean = run(server)
    assert clean["state"]["score"] == 104
    assert clean["dom_score"] == "104"

    # D03 注入版本：渲染分 -100，探针仍拿到真实 104
    mutated_dir = tmp_path / "d03"
    shutil.copytree(FIXTURE, mutated_dir)
    target = mutated_dir / "js" / "html_actuator.js"
    text = target.read_text(encoding="utf-8")
    text = text.replace(
        "  this.scoreContainer.textContent = this.score;",
        "  this.scoreContainer.textContent = this.score - 100;",
        1,
    )
    target.write_text(text, encoding="utf-8")

    handler = partial(_QuietHandler, directory=str(mutated_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        bad = run(f"http://127.0.0.1:{httpd.server_address[1]}/index.html")
    finally:
        httpd.shutdown()

    assert bad["state"]["score"] == 104
    assert bad["dom_score"] == "4"
    assert int(bad["dom_score"]) != bad["state"]["score"]
