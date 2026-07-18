#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GUI Web Agent - MCP Server

把本地 GUI-Actor-2B + Playwright 封装成 MCP 工具, 可被 WorkBuddy / Claude 等
支持 MCP 的客户端调用, 用自然语言指挥浏览器(例如: "打开 B站, 播放某视频")。

工具列表:
  navigate(url)            打开网页
  click(instruction)       看当前页面, 点击符合描述的控件
  type_text(instruction,text)  点击输入框并输入文字
  scroll(direction,amount) 滚动页面
  screenshot()             返回当前页面截图(图片)
  get_url()                返回当前网址
  wait(ms)                 等待
  play_video(instruction)  便捷: 点击播放按钮开始播放

运行: python mcp_server.py   (由 MCP 宿主以 stdio 方式拉起)
"""
import io
import os

from mcp.server.fastmcp import FastMCP, Image as MCPImage
import agent

mcp = FastMCP("vision-gui", host=os.getenv("VISION_MCP_HOST", "127.0.0.1"), port=int(os.getenv("VISION_MCP_PORT", "8765")), log_level="INFO")


@mcp.tool()
def navigate(url: str) -> str:
    """打开一个网页 URL。返回最终跳转后的地址。"""
    return agent.navigate(url)


@mcp.tool()
def click(instruction: str, topk: int = 3) -> str:
    """查看当前页面截图, 点击与 instruction 描述相符的界面元素。
    例如: 'click the play button to start the video' /
          'click the search box' / 'click the login button'。
    返回点击的归一化坐标与像素坐标。"""
    return str(agent.click(instruction, topk=topk))


@mcp.tool()
def type_text(instruction: str, text: str) -> str:
    """点击 instruction 描述的输入框, 然后输入 text。"""
    return str(agent.type_text(instruction, text))


@mcp.tool()
def scroll(direction: str = "down", amount: int = 400) -> str:
    """滚动页面。direction: 'down' 或 'up'; amount: 像素。"""
    return str(agent.scroll(direction, amount))


@mcp.tool()
def screenshot() -> MCPImage:
    """返回当前页面的截图(图片), 供你观察页面状态。"""
    img = agent.screenshot_pil()
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return MCPImage(data=buf.getvalue(), format="png")


@mcp.tool()
def get_url() -> str:
    """返回当前浏览器所在的网址。"""
    return agent.get_url()


@mcp.tool()
def wait(ms: int = 1000) -> str:
    """等待指定毫秒数, 让页面加载/动画完成。"""
    agent.wait(ms)
    return f"waited {ms} ms"


@mcp.tool()
def play_video(instruction: str = "click the play button to start the video") -> str:
    """便捷工具: 找到并点击播放按钮, 开始播放视频。"""
    return str(agent.play_video(instruction))


@mcp.tool()
def desktop_screenshot() -> MCPImage:
    """只返回 Windows 主显示器截图，供模型观察当前电脑界面。"""
    img = agent.desktop_screenshot_pil(); buf = io.BytesIO(); img.save(buf, "PNG")
    return MCPImage(data=buf.getvalue(), format="png")


@mcp.tool()
def list_windows(title_contains: str = "") -> str:
    """检测当前可见窗口及其进程 PID、标题和边界；可按标题关键词过滤。"""
    return str(agent.list_windows(title_contains))


@mcp.tool()
def activate_window(title_contains: str) -> str:
    """按标题关键词找到目标窗口，将其恢复并切换到前台。"""
    return str(agent.activate_window(title_contains))


@mcp.tool()
def window_screenshot(title_contains: str) -> MCPImage:
    """只截取指定标题的目标窗口，避免识别整张主屏幕。"""
    img = agent.window_screenshot_pil(title_contains); buf = io.BytesIO(); img.save(buf, "PNG")
    return MCPImage(data=buf.getvalue(), format="png")


@mcp.tool()
def window_click(title_contains: str, instruction: str, topk: int = 3) -> str:
    """激活目标窗口，在窗口截图内识别控件并点击。"""
    return str(agent.window_click(title_contains, instruction, topk=topk))


@mcp.tool()
def window_type_text(title_contains: str, instruction: str, text: str) -> str:
    """在指定窗口内识别输入框、点击并输入文字。"""
    return str(agent.window_type_text(title_contains, instruction, text))


@mcp.tool()
def desktop_click(instruction: str, topk: int = 3) -> str:
    """截图 Windows 桌面，用本地视觉模型找到描述的控件并点击。"""
    return str(agent.desktop_click(instruction, topk=topk))


@mcp.tool()
def desktop_type_text(instruction: str, text: str) -> str:
    """在 Windows 桌面视觉定位输入框，点击后输入文字。"""
    return str(agent.desktop_type_text(instruction, text))


@mcp.tool()
def desktop_scroll(direction: str = "down", amount: int = 600) -> str:
    """滚动当前 Windows 桌面活动窗口。"""
    return str(agent.desktop_scroll(direction, amount))


@mcp.tool()
def desktop_hotkey(keys: list[str]) -> str:
    """向 Windows 活动窗口发送快捷键，如 ['ctrl','l']、['alt','f4']。"""
    return str(agent.desktop_hotkey(keys))


if __name__ == "__main__":
    if os.getenv("VISION_PRELOAD_MODEL", "0") == "1": agent.load_model()
    transport = os.getenv("VISION_MCP_TRANSPORT", "stdio").strip().lower()
    mcp.run(transport="streamable-http" if transport in {"http", "streamable-http"} else "stdio")
