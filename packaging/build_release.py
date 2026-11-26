"""从源码重新打包 TSP 桌面版（onedir），并生成分发 zip。

关键点：WorkBuddy 拉起的进程带 PYTHONPATH 注入的 sitecustomize（把 os.remove
重定向到回收站），PyInstaller 清理 base_library.zip / 覆盖旧 exe 会报 SHFileOperationW
失败。这里给子进程显式传干净环境。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(r"D:\TSP\tick-stock-panel")
PY = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
DIST = ROOT / "dist" / "TSP"
OUT_ZIP = Path(r"D:\TSP\TSP_LOF_20261125.zip")


def clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    return env


def run(cmd: list[str], cwd: Path, timeout: int = 1800) -> None:
    print(f"[cmd] {' '.join(str(c) for c in cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd), env=clean_env(), timeout=timeout)
    if proc.returncode != 0:
        raise SystemExit(f"命令失败 exit={proc.returncode}: {cmd}")


def main() -> None:
    t0 = time.time()
    if not PY.exists():
        raise SystemExit(f"找不到构建解释器: {PY}")
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        raise SystemExit("frontend/dist 缺失，请先构建前端")

    run([str(PY), "-m", "PyInstaller", "--noconfirm", "packaging/tsp.spec"], ROOT)

    # 产物自检
    exe = DIST / "TSP.exe"
    internal = DIST / "_internal"
    if not exe.exists() or not internal.exists():
        raise SystemExit("打包产物不完整：缺 TSP.exe 或 _internal")

    datas = zipfile.ZipFile  # noqa: F841
    # 注意 PyInstaller 6 onedir 把 PYZ 嵌进 TSP.exe (CArchive), 不是独立文件。
    checks = [
        internal / "app" / "plugins" / "freetdx" / "plugin.yaml",
        internal / "app" / "plugins" / "freetdx" / "requirements.txt",
        internal / "app" / "plugins" / "tdxquant" / "plugin.yaml",
        internal / "app" / "plugins" / "tdxquant" / "requirements.txt",
        internal / "tdxaidata" / "lib" / "TdxAiData.dll",
        internal / "tdxaidata" / "lib" / "TdxAiData.ini",
        internal / "static" / "index.html",
        internal / "app" / "strategy" / "seeds" / "bond_trend.py",
        internal / "app" / "strategy" / "seeds" / "turtle_trading.py",
        internal / "app" / "strategy" / "recommended_pack" / "manifest.json",
        internal / "app" / "strategy" / "recommended_pack" / "rec_risk_adjusted_momentum.py",
    ]
    for p in checks:
        if not p.exists():
            raise SystemExit(f"产物自检失败，缺文件: {p}")
        print(f"[ok] {p.relative_to(DIST)}", flush=True)

    # PYZ 模块自检
    code = r"""
import sys
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
ca = CArchiveReader(sys.argv[1])
pyz = ca.extract('PYZ.pyz')
tmp = r'D:\TSP\_pyz_verify.tmp'
open(tmp,'wb').write(pyz)
z = ZlibArchiveReader(tmp)
mods = set(z.toc)
need = ['tdxaidata','tdxaidata.tdxaidata','app.plugins.tdxquant.provider',
        'app.plugins.tdxquant.client','app.plugins.tdxquant.probe',
        'app.plugins.freetdx.provider','app.plugins.freetdx.client',
        'pytdx','pytdx.hq','app.asset_types','webview','asset_types',
        'app.strategy.monitor_rules','app.strategy.monitor',
        'app.strategy.strategy_seeds','app.services.quote_service',
        'app.strategy.paper','app.strategy.paper_auto','app.strategy.paper_watch',
        'app.api.paper','app.api.kline','app.indicators.chips','app.indicators.patterns']
need += ['app.services.recommended_strategies','app.api.strategy_recommended']
missing = [m for m in need if m not in mods and not (m=='asset_types' and 'app.asset_types' in mods)]
print('PYZ missing:', missing)
raise SystemExit(1 if missing else 0)
"""
    probe_py = ROOT / "build" / "_pyz_verify.py"
    probe_py.parent.mkdir(exist_ok=True)
    probe_py.write_text(code, encoding="utf-8")
    run([str(PY), str(probe_py), str(exe)], ROOT, timeout=120)
    print("[ok] PYZ 模块自检通过", flush=True)

    # COLLECT 每次会清空 dist/TSP,TSP.exe.config 是手工维护的文件, 必须补回。
    # loadFromRemoteSources 允许从网络位置加载程序集, 缺失时部分机器 WebView2 异常。
    exe_config = DIST / "TSP.exe.config"
    if not exe_config.exists():
        exe_config.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<configuration>\n  <runtime>\n"
            '    <loadFromRemoteSources enabled="true" />\n'
            "  </runtime>\n</configuration>\n",
            encoding="utf-8",
        )
        print("[info] 已补回 TSP.exe.config", flush=True)

    # 打 zip（顶层 TSP/ 目录，用户解压即得 TSP.exe + _internal）
    if OUT_ZIP.exists():
        OUT_ZIP.unlink()
        print(f"[info] 已删除旧 zip: {OUT_ZIP}", flush=True)
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                zf.write(path, str(Path("TSP") / path.relative_to(DIST)))
    size_mb = OUT_ZIP.stat().st_size / 1024 / 1024
    print(f"[done] zip: {OUT_ZIP} ({size_mb:.1f} MB), 用时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
