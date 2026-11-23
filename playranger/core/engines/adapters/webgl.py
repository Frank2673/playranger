"""WebGL / Canvas 游戏探测 —— 让 Web 适配器知道自己面对的是哪种渲染面。

为什么单独抽出来：
- DOM 游戏可以直接读节点；Canvas / WebGL 游戏 DOM 几乎是空的
- 如果不区分，引擎会把「空 DOM」误判为「页面没加载」
- 这个模块只做**探测与如实上报**，不臆造状态

用法（Playwright Page）：
    info = detect_web_render_mode(page)
    # {"mode": "webgl2"|"webgl"|"canvas2d"|"dom"|"unknown",
    #  "has_canvas": bool, "canvas_count": int, "renderer": str|None}
"""

from __future__ import annotations

from typing import Any

_DETECT_JS = """
() => {
  const out = {has_canvas: false, canvas_count: 0, webgl: null, webgl2: null,
               renderer: null, dom_nodes: document.querySelectorAll('*').length};
  const canvases = document.querySelectorAll('canvas');
  out.canvas_count = canvases.length;
  out.has_canvas = canvases.length > 0;
  for (const c of canvases) {
    try {
      const gl2 = c.getContext('webgl2');
      if (gl2) {
        out.webgl2 = true;
        const dbg = gl2.getExtension('WEBGL_debug_renderer_info');
        if (dbg) out.renderer = gl2.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
        break;
      }
    } catch (e) {}
    try {
      const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
      if (gl) {
        out.webgl = true;
        const dbg = gl.getExtension('WEBGL_debug_renderer_info');
        if (dbg) out.renderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
        break;
      }
    } catch (e) {}
  }
  return out;
}
"""


def detect_web_render_mode(page: Any, *, dom_threshold: int = 12) -> dict[str, Any]:
    """探测当前页面的渲染模式。

    判定顺序：webgl2 → webgl → canvas2d → dom → unknown。
    DOM 节点数低于阈值时，即使没有 canvas 也标记为 unknown（可能是还没渲染完）。
    """
    result: dict[str, Any] = {
        "mode": "unknown",
        "has_canvas": False,
        "canvas_count": 0,
        "renderer": None,
        "dom_nodes": 0,
        "error": None,
    }
    try:
        data = page.evaluate(_DETECT_JS)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    result.update(
        {
            "has_canvas": bool(data.get("has_canvas")),
            "canvas_count": int(data.get("canvas_count") or 0),
            "renderer": data.get("renderer"),
            "dom_nodes": int(data.get("dom_nodes") or 0),
        }
    )

    if data.get("webgl2"):
        result["mode"] = "webgl2"
    elif data.get("webgl"):
        result["mode"] = "webgl"
    elif data.get("has_canvas"):
        # 有 canvas 但没有 WebGL 上下文 → 大概率 2D canvas
        result["mode"] = "canvas2d"
    elif result["dom_nodes"] >= dom_threshold:
        result["mode"] = "dom"
    else:
        result["mode"] = "unknown"

    return result


def is_vision_required(mode_info: dict[str, Any]) -> bool:
    """该渲染模式是否必须走「截图 + VLM」而不是 DOM 断言。"""
    return mode_info.get("mode") in ("webgl2", "webgl", "canvas2d", "unknown")


__all__ = ["detect_web_render_mode", "is_vision_required"]
