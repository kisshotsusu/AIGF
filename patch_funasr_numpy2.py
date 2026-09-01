#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
为 funasr 打 numpy 2.x 兼容补丁 (幂等, 可重复运行)。

背景:
  funasr (截至 1.3.x) 在 numpy >= 2.0 下会因使用已删除的裸别名而崩溃:
    - np.float  (frontends/default.py 的 CMVN 加载)
    - np.int    (emotion2vec 的 fairseq_modules.py)
    - 以及 np.complex / np.bool / np.object / np.str / np.long / np.unicode
  numpy 2.0 起这些无下划线别名被删除, 仅保留 np.float_ / np.int_ 等。

本脚本把 funasr 包内所有"裸别名"替换为 numpy2 安全写法, 仅匹配 np.X 后面
不是下划线/数字的形态 (np.float_ / np.float64 等不会被二次替换), 因此幂等。

运行方式 (由 set_env.bat 自动调用, 也可手动):
    python patch_funasr_numpy2.py
"""
import pathlib
import re
import sys

FUN = pathlib.Path(sys.prefix) / "Lib" / "site-packages" / "funasr"

if not FUN.exists():
    print("[SKIP] funasr not installed under", FUN, "-> nothing to patch")
    sys.exit(0)

# 裸别名 (np.X 后面不是 _ 或数字) -> numpy2 安全写法
ALIASES = {
    r"np\.float\b(?!_)": "np.float64",
    r"np\.int\b(?!_)": "np.int64",
    r"np\.complex\b(?!_)": "np.complex128",
    r"np\.bool\b(?!_)": "np.bool_",
    r"np\.object\b(?!_)": "np.object_",
    r"np\.str\b(?!_)": "np.str_",
    r"np\.long\b(?!_)": "np.int64",
    r"np\.unicode\b(?!_)": "np.str_",
}

compiled = [(re.compile(p), rep) for p, rep in ALIASES.items()]

patched_files = 0
total_subs = 0
for f in FUN.rglob("*.py"):
    try:
        text = f.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] cannot read {f.relative_to(FUN)}: {e}")
        continue
    new = text
    for rx, rep in compiled:
        new, n = rx.subn(rep, new)
        total_subs += n
    if new != text:
        f.write_text(new, encoding="utf-8")
        patched_files += 1
        print(f"[PATCH] {f.relative_to(FUN)}")

print(
    f"[OK] funasr numpy2 compat: {patched_files} file(s) patched, "
    f"{total_subs} substitution(s)"
)
