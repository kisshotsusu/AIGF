import os, sys, subprocess, importlib.util

DUMPBIN = r"C:\Program Files\Microsoft Visual Studio\18\Community\VC\Tools\MSVC\14.50.35717\bin\Hostx64\x64\dumpbin.exe"

captured = []

import triton.runtime.build as tb

orig_load = tb._load_module_from_path
def patched_load(name, cache_path):
    captured.append(cache_path)
    # 打印依赖诊断信息
    print(f"[diag] 编译产物路径: {cache_path}")
    try:
        print(f"[diag] 文件存在: {os.path.exists(cache_path)} 大小: {os.path.getsize(cache_path) if os.path.exists(cache_path) else 'N/A'}")
    except Exception as e:
        print(f"[diag] stat err: {e}")
    return orig_load(name, cache_path)
tb._load_module_from_path = patched_load

print("[diag] 触发 CudaUtils 编译 ...")
try:
    from triton.backends.nvidia.driver import CudaUtils
    CudaUtils()
    print("[diag] CudaUtils 加载成功")
except Exception as e:
    print(f"[diag] CudaUtils 失败: {type(e).__name__}: {e}")

print(f"[diag] 捕获到的 pyd 路径: {captured}")
for p in captured:
    if p and os.path.exists(p):
        print(f"\n[diag] === dumpbin /dependents {p} ===")
        try:
            out = subprocess.run([DUMPBIN, "/dependents", p], capture_output=True, text=True, timeout=60)
            print(out.stdout)
            if out.stderr:
                print("[stderr]", out.stderr[:500])
        except Exception as e:
            print(f"[diag] dumpbin 失败: {e}")
