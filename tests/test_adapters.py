"""多端适配器单测 —— 无硬件依赖，任何 CI 机器可跑。

原则：
- 不启动浏览器、不连真机、不读真实窗口
- 只验证「协议正确 + 失败路径诚实 + 纯函数正确」
- 真机链路由 docs/COMPATIBILITY_多端支持矩阵.md 中的手工命令验证
"""

from __future__ import annotations

import sys

import pytest

from playranger.core.engines.adapters import (
    SUPPORTED_KINDS,
    AdapterUnavailable,
    get_adapter,
    probe_all,
)
from playranger.core.engines.adapters.native_base import (
    dhash,
    frame_signature,
    hamming_distance,
    run_command,
    summarize_ui_nodes,
)
from playranger.core.engines.adapters.webgl import detect_web_render_mode, is_vision_required


# ── 注册表 ────────────────────────────────────────────────────

def test_supported_kinds_cover_all_platforms():
    assert {"web", "android", "windows", "linux", "ios"}.issubset(set(SUPPORTED_KINDS))


def test_unknown_adapter_raises():
    with pytest.raises(AdapterUnavailable) as ei:
        get_adapter("ps5")
    assert "未知适配器类型" in str(ei.value)


def test_web_adapter_requires_engine():
    with pytest.raises(AdapterUnavailable):
        get_adapter("web")


def test_webgl_alias_returns_web_adapter_with_engine():
    sentinel = object()
    adapter = get_adapter("webgl", engine=sentinel)
    assert adapter.name == "web"


def test_unity_altester_is_honest_placeholder():
    adapter = get_adapter("unity-altester")
    caps = adapter.capabilities()
    assert caps["available"] is False
    assert "未接入 AltTester" in caps["reason"]
    with pytest.raises(AdapterUnavailable):
        adapter.get_state()


def test_probe_all_reports_every_kind():
    matrix = probe_all()
    for kind in SUPPORTED_KINDS:
        assert kind in matrix
        assert "available" in matrix[kind]
        # 不可用必须给原因，不允许沉默
        if not matrix[kind]["available"]:
            assert matrix[kind].get("reason")


# ── 各平台探测：形状正确、不抛异常 ────────────────────────────

@pytest.mark.parametrize("kind", ["android", "windows", "linux", "ios"])
def test_native_probe_shape(kind):
    adapter = get_adapter(kind)
    info = adapter.probe()
    assert isinstance(info, dict)
    assert "available" in info
    assert isinstance(info["available"], bool)
    if not info["available"]:
        assert info.get("reason")


def test_android_probe_without_adb_is_explicit(monkeypatch):
    """把 adb 指向不存在的路径，必须明确报「未找到 adb」。"""
    adapter = get_adapter("android")
    adapter.adb = "definitely-not-an-adb-binary-xyz"
    info = adapter.probe()
    assert info["available"] is False
    assert "adb" in info["reason"].lower()


def test_ios_on_non_macos_is_explicit():
    adapter = get_adapter("ios")
    info = adapter.probe()
    if sys.platform != "darwin":
        assert info["available"] is False
        assert "macos" in info["reason"].lower()


def test_linux_on_non_linux_is_explicit():
    adapter = get_adapter("linux")
    info = adapter.probe()
    if sys.platform != "linux":
        assert info["available"] is False
        assert "linux" in info["reason"].lower()


def test_unavailable_adapter_raises_on_state():
    adapter = get_adapter("linux")
    if sys.platform != "linux":
        with pytest.raises(AdapterUnavailable):
            adapter.get_state()


# ── 纯函数 ────────────────────────────────────────────────────

def test_frame_signature_is_stable_and_sensitive():
    a = b"\x89PNG\r\n\x1a\n" + b"A" * 100
    b = b"\x89PNG\r\n\x1a\n" + b"B" * 100
    assert frame_signature(a) == frame_signature(a)
    assert frame_signature(a) != frame_signature(b)
    assert len(frame_signature(a)) == 32


def test_run_command_missing_binary_returns_127():
    res = run_command(["definitely-not-a-real-binary-xyz"])
    assert res.returncode == 127
    assert res.ok is False
    assert "不存在" in res.stderr


def test_run_command_captures_stdout():
    res = run_command([sys.executable, "-c", "print('hello-playranger')"])
    assert res.ok
    assert "hello-playranger" in res.stdout


def test_hamming_distance_symmetry():
    a, b = "0f0f0f0f", "ffffffff"
    assert hamming_distance(a, b) == hamming_distance(b, a)
    assert hamming_distance(a, "bad") is None


def test_dhash_tolerates_missing_pillow(monkeypatch):
    """Pillow 不可用时返回 None，而不是崩溃或伪造哈希。"""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("no PIL")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert dhash(b"not-a-png") is None


def test_summarize_ui_nodes_counts():
    nodes = [
        {"text": "开始", "clickable": True},
        {"text": "", "clickable": False},
        {"text": "设置", "clickable": True},
    ]
    summary = summarize_ui_nodes(nodes)
    assert summary["node_count"] == 3
    assert summary["clickable_count"] == 2
    assert summary["sample_texts"] == ["开始", "设置"]


# ── WebGL 探测 ────────────────────────────────────────────────

class _FakePage:
    def __init__(self, payload):
        self._payload = payload

    def evaluate(self, _js):
        return self._payload


def test_detect_webgl2():
    page = _FakePage({"has_canvas": True, "canvas_count": 1, "webgl2": True,
                      "webgl": True, "renderer": "ANGLE (NVIDIA)", "dom_nodes": 20})
    info = detect_web_render_mode(page)
    assert info["mode"] == "webgl2"
    assert info["renderer"] == "ANGLE (NVIDIA)"
    assert is_vision_required(info) is True


def test_detect_canvas2d():
    page = _FakePage({"has_canvas": True, "canvas_count": 1, "webgl2": False,
                      "webgl": False, "renderer": None, "dom_nodes": 30})
    info = detect_web_render_mode(page)
    assert info["mode"] == "canvas2d"
    assert is_vision_required(info) is True


def test_detect_dom_game():
    page = _FakePage({"has_canvas": False, "canvas_count": 0, "webgl2": False,
                      "webgl": False, "renderer": None, "dom_nodes": 500})
    info = detect_web_render_mode(page)
    assert info["mode"] == "dom"
    assert is_vision_required(info) is False


def test_detect_unknown_when_page_not_ready():
    page = _FakePage({"has_canvas": False, "canvas_count": 0, "webgl2": False,
                      "webgl": False, "renderer": None, "dom_nodes": 2})
    info = detect_web_render_mode(page)
    assert info["mode"] == "unknown"
    assert is_vision_required(info) is True


def test_detect_handles_evaluate_error():
    class _Broken:
        def evaluate(self, _js):
            raise RuntimeError("page crashed")

    info = detect_web_render_mode(_Broken())
    assert info["mode"] == "unknown"
    assert "page crashed" in info["error"]
