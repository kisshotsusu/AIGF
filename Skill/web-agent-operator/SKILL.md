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

1. Use the semantic task plan to obtain `site`, `query`, qualifiers, requested action, and observable success condition. Application code may validate these fields but must not re-extract the target with site-specific regular expressions. If the semantic plan cannot provide the query, ask instead of opening an empty homepage.
   - Parse action chains such as “搜索并播放 X” as `query=X`; never return connectors such as “并/然后/再” as the query.
   - Preserve titles containing connector characters. Only treat a connector as syntax when it is directly attached to another action verb.
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

For a request such as “打开收藏夹 二次元好看 的第三个视频”, keep semantic planning in the model but execute the fragile account operation through `bilibili_open_favorite_video(favorite_folder="二次元好看", index=3)` when that tool is available. Pass the planned folder and index without re-parsing them locally. Require all of these fields before reporting success:

- `ok=true`
- `used_existing_browser=true`
- `favorite_index` equals the requested index
- a valid `bvid`
- the final URL contains that `bvid`

This executor reads Bilibili's real favorite-folder order and navigates the user's already-open browser. Do not replace it with visual guesses such as opening the avatar, dynamic feed, or an assumed `/account/favorite` URL.

## Guardrails

- Prefer live DOM/HTML because it exposes exact text, links, buttons, inputs, labels, and URLs.
- DOM access to a normal Chrome/Edge session requires its CDP debugging endpoint. If unavailable, keep that browser session and fall back to window vision.
- For tasks that depend on an existing login, never launch Playwright Chromium, a temporary profile, or a new Chrome/Edge/Firefox instance. If no normal browser window exists, stop and say that an existing browser is required.
- Never close a user browser connected through CDP.
- Do not use desktop vision when `inspect_active_target` reports `browser_dom` unless DOM operations fail twice or the relevant UI is canvas/video-only.
- Do not use GUI-Actor, screenshots, coordinate clicks, or desktop/window image tools while GUI recognition is disabled.
- Do not stop after `navigate` unless opening the page is the complete request.
- Do not treat a browser process launch, tab creation, HTTP 200, or homepage title as evidence that search or selection finished.
- Bound retries: at most two alternatives per failed step.
- Preserve the persistent MCP service; do not start a new model process.
