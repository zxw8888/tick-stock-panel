"""推荐策略包 — 清单读取、安装状态与安全落盘。

推荐策略随程序分发，但安装目标仍是 ``data/strategies/custom``，由用户显式
加入策略池。自动安装只补齐“从未安装过”的条目：
  - 已存在的同名文件不覆盖（可能是用户自己的策略）
  - 用户删除过的文件不复活（安装记录仍在）
  - 新包版本只补新增 ID，不自动覆盖旧版本
显式覆盖安装会先备份原文件到 ``data/user_data/recommended_strategy_backups``。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STATE_VERSION = 1
_STATE_REL = Path("user_data") / "recommended_strategies.json"
_BACKUP_REL = Path("user_data") / "recommended_strategy_backups"
_INSTALL_REL = Path("strategies") / "custom"


def pack_dir() -> Path:
    """推荐策略包目录（源码与 PyInstaller 冻结包均可用）。"""
    return Path(__file__).resolve().parents[1] / "strategy" / "recommended_pack"


def load_manifest() -> dict[str, Any]:
    path = pack_dir() / "manifest.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"推荐策略清单读取失败: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("推荐策略清单必须是 JSON 对象")
    pack_version = raw.get("pack_version")
    strategies = raw.get("strategies")
    if not isinstance(pack_version, str) or not pack_version:
        raise ValueError("推荐策略清单缺少 pack_version")
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("推荐策略清单缺少 strategies")

    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, item in enumerate(strategies):
        if not isinstance(item, dict):
            raise ValueError(f"推荐策略清单 strategies[{index}] 必须是对象")
        sid = item.get("id")
        file_name = item.get("file")
        if not isinstance(sid, str) or not sid.startswith("rec_"):
            raise ValueError(f"推荐策略清单 strategies[{index}] 的 id 非法")
        if sid in seen_ids:
            raise ValueError(f"推荐策略清单重复 id: {sid}")
        if not isinstance(file_name, str) or not file_name.endswith(".py"):
            raise ValueError(f"推荐策略 {sid} 的 file 非法")
        if Path(file_name).name != file_name or "/" in file_name or "\\" in file_name:
            raise ValueError(f"推荐策略 {sid} 的 file 必须是当前目录下的文件名")
        if file_name in seen_files:
            raise ValueError(f"推荐策略清单重复 file: {file_name}")
        seen_ids.add(sid)
        seen_files.add(file_name)
        items.append({**item, "id": sid, "file": file_name})

    return {"pack_version": pack_version, "strategies": items}


def _state_path(data_dir: Path) -> Path:
    return data_dir / _STATE_REL


def _install_dir(data_dir: Path) -> Path:
    return data_dir / _INSTALL_REL


def _empty_state() -> dict[str, Any]:
    return {
        "version": _STATE_VERSION,
        "installed": {},
        "prompt_pending": [],
    }


def _load_state(data_dir: Path) -> dict[str, Any]:
    path = _state_path(data_dir)
    if not path.is_file():
        return _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("recommended strategies state unreadable, using empty state: %s", exc)
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    installed = raw.get("installed")
    if not isinstance(installed, dict):
        installed = {}
    normalized: dict[str, dict[str, Any]] = {}
    for sid, record in installed.items():
        if not isinstance(sid, str) or not isinstance(record, dict):
            continue
        digest = record.get("sha256")
        if not isinstance(digest, str) or not digest:
            continue
        normalized[sid] = {
            "version": str(record.get("version") or ""),
            "sha256": digest,
            "installed_at": str(record.get("installed_at") or ""),
            "pack_version": str(record.get("pack_version") or ""),
        }
    pending = list(dict.fromkeys(
        sid for sid in (raw.get("prompt_pending") or [])
        if isinstance(sid, str) and sid
    ))
    return {"version": _STATE_VERSION, "installed": normalized, "prompt_pending": pending}


def _write_state(data_dir: Path, state: dict[str, Any]) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _STATE_VERSION,
        "installed": state.get("installed") or {},
        "prompt_pending": state.get("prompt_pending") or [],
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _item_status(
    item: dict[str, Any],
    record: dict[str, Any] | None,
    target: Path,
) -> str:
    file_exists = target.is_file()
    if record is None:
        return "conflict" if file_exists else "not_installed"
    if not file_exists:
        return "missing"
    if _sha256(target) != record.get("sha256"):
        return "modified"
    if record.get("version") != item.get("version"):
        return "update_available"
    return "installed"


def _status_payload(
    item: dict[str, Any],
    record: dict[str, Any] | None,
    target: Path,
) -> dict[str, Any]:
    status = _item_status(item, record, target)
    return {
        **item,
        "status": status,
        "installed_path": str(target),
        "installed_version": (record or {}).get("version"),
        "installed_at": (record or {}).get("installed_at"),
        "installed_pack_version": (record or {}).get("pack_version"),
    }


def list_recommended(data_dir: Path) -> dict[str, Any]:
    """返回推荐策略清单及当前安装状态，不写任何文件。"""
    manifest = load_manifest()
    state = _load_state(data_dir)
    install_dir = _install_dir(data_dir)
    items: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for item in manifest["strategies"]:
        record = state["installed"].get(item["id"])
        target = install_dir / item["file"]
        payload = _status_payload(item, record, target)
        items.append(payload)
        by_id[item["id"]] = payload

    pending = [
        sid for sid in state["prompt_pending"]
        if sid in by_id and by_id[sid]["status"] in {"installed", "update_available", "modified"}
    ]
    return {
        "pack_version": manifest["pack_version"],
        "prompt_pending_ids": pending,
        "items": items,
    }


def _validate_code(item: dict[str, Any], code: str) -> None:
    from app.strategy.ai_generator import AIStrategyGenerator

    try:
        AIStrategyGenerator._validate_safety(code)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f"推荐策略 {item['id']} 未通过安全检查: {exc}") from exc


def _backup_targets(data_dir: Path, targets: list[Path]) -> Path | None:
    existing = [path for path in targets if path.is_file()]
    if not existing:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = data_dir / _BACKUP_REL / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in existing:
        shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def _install_one(
    item: dict[str, Any],
    *,
    install_dir: Path,
    overwrite: bool,
) -> tuple[Path, str, bool]:
    source = pack_dir() / item["file"]
    try:
        code_bytes = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"推荐策略源文件缺失: {source}") from exc
    try:
        code = code_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"推荐策略 {item['id']} 不是 UTF-8 文本") from exc
    _validate_code(item, code)
    target = install_dir / item["file"]
    if target.is_file():
        if target.read_bytes() == code_bytes:
            return target, _sha256_bytes(code_bytes), False
        if not overwrite:
            raise RecommendedConflictError([item["id"]])
    install_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(target, code_bytes)
    return target, _sha256_bytes(code_bytes), True


class RecommendedConflictError(ValueError):
    """目标文件已存在且内容不同，需要显式覆盖。"""

    def __init__(self, ids: list[str]):
        self.ids = list(ids)
        super().__init__(f"以下推荐策略文件已存在，需要覆盖安装: {', '.join(self.ids)}")


def _rollback_files(originals: dict[Path, bytes | None]) -> None:
    for path, content in originals.items():
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, content)
        except OSError as exc:
            logger.error("recommended strategy rollback failed for %s: %s", path, exc)


def install_recommended(
    data_dir: Path,
    ids: list[str] | None = None,
    *,
    overwrite: bool = False,
    engine: Any | None = None,
) -> dict[str, Any]:
    """显式安装/更新推荐策略；失败时回滚本次写入。"""
    manifest = load_manifest()
    by_id = {item["id"]: item for item in manifest["strategies"]}
    selected = list(by_id) if not ids else list(dict.fromkeys(ids))
    unknown = [sid for sid in selected if sid not in by_id]
    if unknown:
        raise ValueError(f"未知推荐策略: {', '.join(unknown)}")

    install_dir = _install_dir(data_dir)
    targets = [install_dir / by_id[sid]["file"] for sid in selected]

    with _LOCK:
        state = _load_state(data_dir)
        originals: dict[Path, bytes | None] = {}
        for target in targets:
            originals[target] = target.read_bytes() if target.is_file() else None
        backup_dir = _backup_targets(data_dir, targets) if overwrite else None
        previous_state = json.loads(json.dumps(state, ensure_ascii=False))
        written: list[str] = []
        try:
            for sid in selected:
                item = by_id[sid]
                _target, digest, changed = _install_one(
                    item, install_dir=install_dir, overwrite=overwrite,
                )
                if changed:
                    written.append(sid)
                state["installed"][sid] = {
                    "version": str(item.get("version") or ""),
                    "sha256": digest,
                    "installed_at": datetime.now().isoformat(timespec="seconds"),
                    "pack_version": manifest["pack_version"],
                }
            _write_state(data_dir, state)
            if engine is not None and selected:
                engine.reload()
        except Exception:
            _rollback_files(originals)
            try:
                _write_state(data_dir, previous_state)
            except OSError as exc:
                logger.error("recommended strategy state rollback failed: %s", exc)
            if engine is not None:
                try:
                    engine.reload()
                except Exception as exc:  # noqa: BLE001
                    logger.error("recommended strategy engine rollback reload failed: %s", exc)
            raise

        refreshed = list_recommended(data_dir)
        refreshed["installed_ids"] = written
        refreshed["backup_dir"] = str(backup_dir) if backup_dir else None
        return refreshed


def auto_install_recommended(data_dir: Path) -> dict[str, Any]:
    """启动时补齐从未安装过的推荐策略；不覆盖、不复活已删除文件。"""
    manifest = load_manifest()
    install_dir = _install_dir(data_dir)
    with _LOCK:
        state = _load_state(data_dir)
        previous_state = json.loads(json.dumps(state, ensure_ascii=False))
        originals: dict[Path, bytes | None] = {}
        installed: list[str] = []
        try:
            for item in manifest["strategies"]:
                sid = item["id"]
                if sid in state["installed"]:
                    continue
                target = install_dir / item["file"]
                if target.exists():
                    continue
                originals[target] = None
                source = pack_dir() / item["file"]
                code_bytes = source.read_bytes()
                code = code_bytes.decode("utf-8")
                _validate_code(item, code)
                install_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write_bytes(target, code_bytes)
                state["installed"][sid] = {
                    "version": str(item.get("version") or ""),
                    "sha256": _sha256_bytes(code_bytes),
                    "installed_at": datetime.now().isoformat(timespec="seconds"),
                    "pack_version": manifest["pack_version"],
                }
                if sid not in state["prompt_pending"]:
                    state["prompt_pending"].append(sid)
                installed.append(sid)
            if installed:
                _write_state(data_dir, state)
        except Exception:
            _rollback_files(originals)
            try:
                _write_state(data_dir, previous_state)
            except OSError as exc:
                logger.error("recommended strategy auto-install state rollback failed: %s", exc)
            raise
        if installed:
            logger.info("recommended strategies auto-installed: %s", ", ".join(installed))
        return {"pack_version": manifest["pack_version"], "installed_ids": installed}


def ack_prompt(data_dir: Path, ids: list[str] | None = None) -> dict[str, Any]:
    """确认已看过推荐提示；ids 为空时清空全部待提示项。"""
    with _LOCK:
        state = _load_state(data_dir)
        if ids:
            drop = set(ids)
            state["prompt_pending"] = [sid for sid in state["prompt_pending"] if sid not in drop]
        else:
            state["prompt_pending"] = []
        _write_state(data_dir, state)
    return list_recommended(data_dir)
