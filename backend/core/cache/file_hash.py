"""FileHashCache — mtime+size 快速变化检测 + SHA256 缓存。

避免 daemon 每次 workspace_dirty 都重新计算所有文件的 SHA-256。
持久化到 .gitgo/file_hashes.json，内存 LRU 热缓存 500 条。

v0.31: hit/miss 统计 + access-time LRU + TTL (1hr)
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path


class FileHashCache:
    """文件哈希缓存。mtime+size 匹配则返回缓存 SHA-256，跳过重算。"""

    MAX_HOT = 500
    TTL_SECONDS = 3600  # 1 小时无命中则淘汰

    def __init__(self, cache_dir: Path):
        self._dir = Path(cache_dir)
        self._path = self._dir / "file_hashes.json"
        self._cold: dict[str, dict] = {}  # 从磁盘加载的全量
        self._hot: dict[str, dict] = {}   # 本次 session 新增/命中的条目
        self._dirty = False
        self._hits: int = 0
        self._misses: int = 0
        self._load()

    # ── Public API ──────────────────────────────────────────

    def lookup(self, rel_path: str, mtime: float, size: int) -> str | None:
        """mtime + size 匹配 → 返回缓存 sha256；不匹配 → None。

        命中时更新 cached_at（access-time LRU）。
        检查 TTL，过期条目视为 miss。
        """
        entry = self._hot.get(rel_path) or self._cold.get(rel_path)
        if entry is None:
            self._misses += 1
            return None

        # TTL 检查
        cached_at_str = entry.get("cached_at", "")
        if cached_at_str:
            try:
                cached_dt = datetime.fromisoformat(cached_at_str)
                age = (datetime.now() - cached_dt).total_seconds()
                if age > self.TTL_SECONDS:
                    self._misses += 1
                    self.invalidate(rel_path)
                    return None
            except (ValueError, TypeError):
                pass

        if entry["mtime"] == mtime and entry["size"] == size:
            # access-time LRU: 更新访问时间
            entry["cached_at"] = datetime.now().isoformat()
            if rel_path in self._cold:
                self._hot[rel_path] = entry
                del self._cold[rel_path]
                self._dirty = True
            self._hits += 1
            return entry["sha256"]

        self._misses += 1
        return None

    def store(self, rel_path: str, mtime: float, size: int, sha256: str):
        """写入热缓存。"""
        self._hot[rel_path] = {
            "mtime": mtime, "size": size, "sha256": sha256,
            "cached_at": datetime.now().isoformat(),
        }
        self._dirty = True
        self._maybe_evict()

    def invalidate(self, rel_path: str):
        """Watcher 检测到文件变化时主动删除缓存条目。"""
        self._hot.pop(rel_path, None)
        if rel_path in self._cold:
            del self._cold[rel_path]
            self._dirty = True

    def flush(self):
        """持久化到磁盘。合并 hot → cold，LRU 淘汰后写入 JSON。"""
        if not self._dirty:
            return
        merged = {**self._cold, **self._hot}
        if len(merged) > self.MAX_HOT:
            # access-time LRU: 按 cached_at 降序，保留最近访问的
            merged = dict(sorted(
                merged.items(),
                key=lambda kv: kv[1].get("cached_at", ""),
                reverse=True,
            )[:self.MAX_HOT])
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
        self._cold = merged
        self._hot.clear()
        self._dirty = False

    def stats(self) -> dict:
        """返回缓存统计信息。"""
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "total_requests": total,
            "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
            "hot_entries": len(self._hot),
            "cold_entries": len(self._cold),
            "total_entries": len(self._hot) + len(self._cold),
            "max_entries": self.MAX_HOT,
            "ttl_seconds": self.TTL_SECONDS,
        }

    # ── Internal ────────────────────────────────────────────

    def _load(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self._cold = dict(list(data.items())[:self.MAX_HOT])
        except (json.JSONDecodeError, OSError):
            self._cold = {}

    def _maybe_evict(self):
        if len(self._hot) >= self.MAX_HOT:
            self.flush()
