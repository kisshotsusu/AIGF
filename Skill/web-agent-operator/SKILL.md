---
name: web-agent-operator
description: Complete outcome-oriented webpage tasks by reading and operating the current browser page DOM/HTML first, then falling back to browser-window vision or desktop vision only when DOM access is unavailable or the active program is not a webpage. Use for website search, selection, navigation, media, forms, clicks, submission, and verification, especially tasks that must preserve the user's signed-in browser session.
---

# Web Agent Operator

Treat the user's requested outcome as the completion condition. Opening a site is only navigation unless opening is the entire request.

## Completion contract

Classify the request before acting:

- **Open-only**: “打开 B站首页”. Complete after `navigate` and `get_url` verification.
- **Search-only**: “在淘宝搜索机械键盘”. Complete only when the result page visibly contains the query or relevant results.
- **Search-and-open**: “去知乎找某问题并打开”. Complete only after selecting a relevant result and verifying the destination.
- **Transactional/media**: “播放、填写、提交、加入购物车”. Complete only after verifying that final action's state.

Never reinterpret a multi-step request as open-only. Never call `open_url` and report success when any verb remains unfinished.

## Workflow

1. Extract `site`, `query`, qualifiers, requested action, and observable success condition. If the query cannot be extracted, ask instead of opening an empty homepage.
2. Call `inspect_active_target` before acting.
   - `browser_dom`: call `web_read` immediately and operate the current signed-in page through DOM/text tools.
   - `browser_visual`: keep the existing browser and use `window_screenshot`, `window_click`, and `window_type_text`; do not launch another browser merely to obtain DOM access.
   - `desktop_visual`: the active program is not a supported webpage; use window/desktop vision tools.
3. Navigate directly to a safe search URL only when the current page cannot reach the requested site; otherwise locate the current page search input from `web_read`, then `web_fill(..., submit=true)`.
4. Verify the search actually happened with `get_url` and `web_read` when DOM is available; otherwise verify from a fresh window screenshot.
5. Rank results using all meaningful query terms. Prefer a candidate matching more qualifiers; do not blindly select the first item.
6. Open the selected result with DOM tools when available, then verify the URL/title/body changed. Use visual coordinates only in a visual fallback mode.
7. Perform the requested final action and verify its resulting state. Report success only with observable evidence.

Use this state sequence and do not skip states:

`NAVIGATED → SEARCH_SUBMITTED → RESULTS_VERIFIED → RESULT_SELECTED → ACTION_EXECUTED → FINAL_VERIFIED`

If a state fails, retry with at most two sensible alternatives. Return the failed state and actual reason; never convert partial progress into success.

## Bilibili

Use the deterministic script for search-and-play tasks:

```powershell
..\..\.venv\Scripts\python.exe scripts\web_agent.py --site bilibili --query "hanser 芒种" --action play
```

The script emits JSON progress events and ends with `completed` only after the selected page and requested playback state are verified.

## Guardrails

- Prefer live DOM/HTML because it exposes exact text, links, buttons, inputs, labels, and URLs.
- DOM access to a normal Chrome/Edge session requires its CDP debugging endpoint. If unavailable, keep that browser session and fall back to window vision.
- Never close a user browser connected through CDP.
- Do not use desktop vision when `inspect_active_target` reports `browser_dom` unless DOM operations fail twice or the relevant UI is canvas/video-only.
- Do not use GUI-Actor, screenshots, coordinate clicks, or desktop/window image tools while GUI recognition is disabled.
- Do not stop after `navigate` unless opening the page is the complete request.
- Do not treat a browser process launch, tab creation, HTTP 200, or homepage title as evidence that search or selection finished.
- Bound retries: at most two alternatives per failed step.
- Preserve the persistent MCP service; do not start a new model process.
