---
name: web-agent-operator
description: Complete outcome-oriented, multi-step webpage tasks through DOM and text tools without image GUI recognition. Use whenever the user asks to open or visit a website and then search, find, select, compare, navigate, play media, fill a form, click, submit, or verify something. Prevents stopping after merely opening a homepage; especially use for Bilibili search-and-play requests.
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
2. Navigate directly to a safe search URL when the site supports one; otherwise `navigate` to the site, `web_read`, locate the search input, then `web_fill(..., submit=true)`.
3. Verify the search actually happened with `get_url` and `web_read`. The URL, input value, page title, or result text must contain the query or unmistakably relevant results.
4. Rank results using all meaningful query terms. Prefer a candidate matching more qualifiers; do not blindly select the first item.
5. Open the selected result with `web_click_text` or `navigate`, then verify the URL/title/body changed to the intended destination.
6. Perform the requested final action such as `web_play_media`, form fill, click, or submit.
7. Call `get_url` and `web_read` after the final action. Report success only with observable evidence.

Use this state sequence and do not skip states:

`NAVIGATED → SEARCH_SUBMITTED → RESULTS_VERIFIED → RESULT_SELECTED → ACTION_EXECUTED → FINAL_VERIFIED`

If a state fails, retry with at most two sensible alternatives. Return the failed state and actual reason; never convert partial progress into success.

## Bilibili

Use the deterministic script for search-and-play tasks:

```powershell
E:\Doc\AI直播\.venv\Scripts\python.exe scripts\web_agent.py --site bilibili --query "hanser 芒种" --action play
```

The script emits JSON progress events and ends with `completed` only after the selected page and requested playback state are verified.

## Guardrails

- Do not use GUI-Actor, screenshots, coordinate clicks, or desktop/window image tools while GUI recognition is disabled.
- Do not stop after `navigate` unless opening the page is the complete request.
- Do not treat a browser process launch, tab creation, HTTP 200, or homepage title as evidence that search or selection finished.
- Bound retries: at most two alternatives per failed step.
- Preserve the persistent MCP service; do not start a new model process.
