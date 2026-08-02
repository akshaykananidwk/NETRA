"""One-click GitHub updater.

Configure once (repo + branch + token, stored in the settings table), then:

  check()  — compare the local commit against GitHub and report what's new
             (latest commit message / author / date, commits behind).
  run()    — download the branch zipball, BACKUP current code + DB, apply
             files (never touching protected paths like .env, data/, models/),
             run idempotent schema + pending migrations/*.sql, clear
             __pycache__, record the new commit, then restart the process.
             Any failure after apply triggers automatic rollback from the
             backup before restarting.

The process exits after a successful update (or a rollback); start.bat's
restart loop — or NSSM/systemd — brings it back up on the new code.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import requests

log = logging.getLogger("krishna.updater")

BASE_DIR = Path(__file__).resolve().parent.parent

CFG_REPO = "github_repo"
CFG_BRANCH = "github_branch"
CFG_TOKEN = "github_token"
CFG_COMMIT = "app_commit"
CFG_UPDATED_AT = "app_updated_at"

GH_API = "https://api.github.com"


class UpdateError(Exception):
    pass


class UpdateManager:
    # never overwritten / deleted by an update
    PROTECTED_PREFIXES = ("data/", "logs/", "models/", "venv/", ".venv/",
                          ".git/", "__pycache__/")
    PROTECTED_FILES = (".env", "config.php")
    # live batch files: cmd re-reads them mid-run — replacing them while the
    # restart loop executes corrupts it, so updates land next to them as .new
    LIVE_BATCH = ("start.bat", "install.bat")

    def __init__(self, app_root: Optional[Path] = None, bus=None) -> None:
        self.app_root = Path(app_root or BASE_DIR)
        self.bus = bus
        self.running = False
        self.log: List[dict] = []
        self._lock = threading.Lock()

    # ── configuration ─────────────────────────────────────────────────────
    def _get_setting(self, key: str) -> Optional[str]:
        from core.db import SessionLocal, Setting
        with SessionLocal() as s:
            row = s.get(Setting, key)
            return row.value if row else None

    def _set_setting(self, key: str, value: str) -> None:
        from core.db import SessionLocal, Setting
        with SessionLocal() as s:
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=value))
            else:
                row.value = value
            s.commit()

    def get_config(self, mask_token: bool = True) -> dict:
        token = self._get_setting(CFG_TOKEN) or ""
        return {
            "repo": self._get_setting(CFG_REPO) or "",
            "branch": self._get_setting(CFG_BRANCH) or "main",
            "token_set": bool(token),
            "token": (("•" * 8 + token[-4:]) if (token and mask_token) else token),
            "current_commit": (self.current_commit() or "")[:12],
            "last_update_at": self._get_setting(CFG_UPDATED_AT) or "",
        }

    def save_config(self, repo: str, branch: str,
                    token: Optional[str] = None) -> None:
        repo = repo.strip().removeprefix("https://github.com/").strip("/")
        if repo.count("/") != 1:
            raise UpdateError("repo must look like owner/repo")
        self._set_setting(CFG_REPO, repo)
        self._set_setting(CFG_BRANCH, branch.strip() or "main")
        if token is not None and token.strip():
            self._set_setting(CFG_TOKEN, token.strip())
        from core.db import audit
        audit("web", "update.config", f"repo={repo} branch={branch}")

    # ── version helpers ───────────────────────────────────────────────────
    def _detect_git_commit(self) -> Optional[str]:
        """Read .git/HEAD without needing the git binary."""
        try:
            head = (self.app_root / ".git" / "HEAD").read_text().strip()
            if head.startswith("ref:"):
                ref = head.split(" ", 1)[1].strip()
                ref_file = self.app_root / ".git" / ref
                if ref_file.exists():
                    return ref_file.read_text().strip()
                packed = self.app_root / ".git" / "packed-refs"
                if packed.exists():
                    for line in packed.read_text().splitlines():
                        if line.endswith(ref):
                            return line.split(" ", 1)[0]
                return None
            return head
        except OSError:
            return None

    def current_commit(self) -> Optional[str]:
        return self._get_setting(CFG_COMMIT) or self._detect_git_commit()

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        token = self._get_setting(CFG_TOKEN)
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    # ── check ─────────────────────────────────────────────────────────────
    def check(self) -> dict:
        cfg = self.get_config()
        if not cfg["repo"]:
            raise UpdateError("પહેલા Repository સેવ કરો (owner/repo)")
        repo, branch = cfg["repo"], cfg["branch"]

        r = requests.get(f"{GH_API}/repos/{repo}/branches/{branch}",
                         headers=self._headers(), timeout=15)
        if r.status_code == 404:
            raise UpdateError(f"Repo/branch મળ્યું નહીં: {repo}@{branch}")
        if r.status_code in (401, 403):
            raise UpdateError("GitHub Token ખોટો છે અથવા access નથી")
        r.raise_for_status()
        head = r.json()["commit"]
        latest = {
            "sha": head["sha"],
            "short": head["sha"][:7],
            "message": (head["commit"]["message"] or "").split("\n")[0][:200],
            "author": head["commit"]["author"]["name"],
            "date": head["commit"]["author"]["date"],
        }

        current = self.current_commit()
        result = {
            "repo": repo, "branch": branch,
            "current_commit": (current or "")[:12],
            "latest": latest,
            "update_available": current != latest["sha"],
            "commits_behind": None,
            "commits": [],
        }
        if current and current != latest["sha"]:
            try:
                c = requests.get(
                    f"{GH_API}/repos/{repo}/compare/{current}...{latest['sha']}",
                    headers=self._headers(), timeout=15)
                if c.ok:
                    data = c.json()
                    result["commits_behind"] = data.get("ahead_by")
                    result["commits"] = [{
                        "short": x["sha"][:7],
                        "message": (x["commit"]["message"] or "").split("\n")[0][:150],
                        "author": x["commit"]["author"]["name"],
                        "date": x["commit"]["author"]["date"],
                    } for x in data.get("commits", [])[-10:]]
            except requests.RequestException:
                pass  # compare is informational only
        return result

    # ── update pipeline ───────────────────────────────────────────────────
    def _step(self, step: str, status: str, detail: str = "") -> None:
        entry = {"step": step, "status": status, "detail": detail,
                 "time": datetime.now().strftime("%H:%M:%S")}
        self.log.append(entry)
        log.info("update[%s] %s %s", step, status, detail)
        if self.bus is not None:
            self.bus.publish("update.progress", **entry)

    def is_protected(self, rel: str) -> bool:
        rel = rel.replace("\\", "/").lstrip("/")
        if rel in self.PROTECTED_FILES:
            return True
        return any(rel == p.rstrip("/") or rel.startswith(p)
                   for p in self.PROTECTED_PREFIXES)

    def _download(self, repo: str, branch: str, staging: Path) -> Path:
        zip_path = staging / "update.zip"
        url = f"{GH_API}/repos/{repo}/zipball/{branch}"
        with requests.get(url, headers=self._headers(), stream=True,
                          timeout=(10, 180)) as r:
            if r.status_code in (401, 403):
                raise UpdateError("GitHub Token ખોટો છે અથવા access નથી")
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        if zip_path.stat().st_size < 200:
            raise UpdateError("ડાઉનલોડ ખાલી છે")
        return zip_path

    def _extract(self, zip_path: Path, staging: Path) -> Path:
        out = staging / "tree"
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                target = (out / info.filename).resolve()
                if not str(target).startswith(str(out.resolve())):
                    raise UpdateError(f"unsafe zip path: {info.filename}")
            z.extractall(out)
        roots = [p for p in out.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise UpdateError("zip માં અપેક્ષિત folder structure નથી")
        return roots[0]

    def _incoming_files(self, tree: Path) -> List[str]:
        return sorted(str(p.relative_to(tree)).replace("\\", "/")
                      for p in tree.rglob("*") if p.is_file())

    def _backup(self, files: List[str], backup_dir: Path) -> dict:
        backup_dir.mkdir(parents=True, exist_ok=True)
        code_zip = backup_dir / "code.zip"
        count = 0
        with zipfile.ZipFile(code_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in files:
                src = self.app_root / rel
                if src.exists() and not self.is_protected(rel):
                    z.write(src, rel)
                    count += 1
        # live sqlite backup (consistent even mid-write, thanks to WAL)
        from core import db as coredb
        db_backup = backup_dir / "krishna.db"
        if coredb.engine is not None:
            src = sqlite3.connect(coredb.engine.url.database)
            dst = sqlite3.connect(str(db_backup))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
        return {"code_zip": str(code_zip), "db_backup": str(db_backup),
                "files": count}

    def _apply(self, tree: Path, files: List[str]) -> dict:
        updated, skipped, deferred = 0, 0, 0
        requirements_changed = False
        for rel in files:
            if self.is_protected(rel):
                skipped += 1
                continue
            src = tree / rel
            dst = self.app_root / rel
            if rel == "requirements.txt" and dst.exists() \
                    and dst.read_bytes() != src.read_bytes():
                requirements_changed = True
            if Path(rel).name.lower() in self.LIVE_BATCH and dst.exists():
                new_content = src.read_bytes()
                if dst.read_bytes() != new_content:
                    (dst.parent / (dst.name + ".new")).write_bytes(new_content)
                    deferred += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.parent / (dst.name + ".updtmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)          # atomic per-file
            updated += 1
        if requirements_changed:
            # start.bat installs new packages on the next boot before the app runs
            flag = self.app_root / "data" / "needs_pip_install"
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(datetime.now().isoformat())
        return {"updated": updated, "protected_skipped": skipped,
                "batch_deferred": deferred,
                "requirements_changed": requirements_changed}

    def _rollback_code(self, code_zip: Path) -> int:
        count = 0
        with zipfile.ZipFile(code_zip) as z:
            for info in z.infolist():
                target = (self.app_root / info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as f:
                    target.write_bytes(f.read())
                count += 1
        return count

    def _restore_db(self, db_backup: Path) -> None:
        from core import db as coredb
        if coredb.engine is None or not db_backup.exists():
            return
        db_file = coredb.engine.url.database
        coredb.engine.dispose()
        src = sqlite3.connect(str(db_backup))
        dst = sqlite3.connect(db_file)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()

    def _migrate(self) -> dict:
        from core import db as coredb
        if coredb.engine is None:
            raise UpdateError("database not initialised")
        db_file = coredb.engine.url.database
        conn = sqlite3.connect(db_file)
        applied = []
        try:
            schema = self.app_root / "core" / "schema.sql"
            if schema.exists():
                conn.executescript(schema.read_text(encoding="utf-8"))
                conn.commit()
            mig_dir = self.app_root / "migrations"
            if mig_dir.exists():
                for sql_file in sorted(mig_dir.glob("*.sql")):
                    key = f"migration:{sql_file.name}"
                    if self._get_setting(key):
                        continue
                    conn.executescript(sql_file.read_text(encoding="utf-8"))
                    conn.commit()
                    self._set_setting(key, datetime.now().isoformat())
                    applied.append(sql_file.name)
        finally:
            conn.close()
        return {"schema": "applied", "migrations": applied}

    def _clear_cache(self) -> int:
        count = 0
        for pyc_dir in self.app_root.rglob("__pycache__"):
            if any(part in ("data", "venv", ".venv", ".git")
                   for part in pyc_dir.parts):
                continue
            shutil.rmtree(pyc_dir, ignore_errors=True)
            count += 1
        return count

    def _schedule_restart(self, delay: float = 1.5) -> None:
        def _exit():
            log.info("restarting for update…")
            os._exit(3)   # start.bat loop / NSSM / systemd restarts us
        threading.Timer(delay, _exit).start()

    # ── the one-click run ─────────────────────────────────────────────────
    def run(self, force: bool = False, restart: bool = True) -> dict:
        if not self._lock.acquire(blocking=False):
            raise UpdateError("Update પહેલેથી ચાલુ છે")
        self.running = True
        self.log = []
        staging = self.app_root / "data" / "staging"
        backup_dir = None
        backup_info = None
        applied = False
        try:
            self._step("check", "run")
            info = self.check()
            latest_sha = info["latest"]["sha"]
            if not info["update_available"] and not force:
                self._step("check", "ok", "પહેલેથી લેટેસ્ટ વર્ઝન છે")
                return {"ok": True, "up_to_date": True, "log": self.log}
            self._step("check", "ok",
                       f"{info['latest']['short']}: {info['latest']['message']}")

            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)

            self._step("download", "run")
            zip_path = self._download(info["repo"], info["branch"], staging)
            self._step("download", "ok",
                       f"{zip_path.stat().st_size // 1024} KB")

            self._step("extract", "run")
            tree = self._extract(zip_path, staging)
            files = self._incoming_files(tree)
            self._step("extract", "ok", f"{len(files)} files")

            self._step("backup", "run")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = self.app_root / "data" / "backups" / f"update_{stamp}"
            backup_info = self._backup(files, backup_dir)
            self._step("backup", "ok",
                       f"{backup_info['files']} files + DB → {backup_dir.name}")

            self._step("apply", "run")
            applied = True
            stats = self._apply(tree, files)
            detail = (f"{stats['updated']} બદલાઈ, "
                      f"{stats['protected_skipped']} સુરક્ષિત skip")
            if stats["batch_deferred"]:
                detail += f", {stats['batch_deferred']} .bat → .new"
            if stats.get("requirements_changed"):
                detail += " — નવાં packages restart પર install થશે"
            self._step("apply", "ok", detail)

            self._step("migrate", "run")
            mig = self._migrate()
            self._step("migrate", "ok",
                       f"schema ok, {len(mig['migrations'])} migration(s)")

            self._step("cache", "run")
            n = self._clear_cache()
            self._step("cache", "ok", f"{n} __pycache__ સાફ")

            self._set_setting(CFG_COMMIT, latest_sha)
            self._set_setting(CFG_UPDATED_AT, datetime.now().isoformat())
            from core.db import audit
            audit("web", "update.run", f"-> {latest_sha[:12]}")
            shutil.rmtree(staging, ignore_errors=True)

            if restart:
                self._step("restart", "run", "સર્વર રીસ્ટાર્ટ થાય છે…")
                self._schedule_restart()
            self._step("done", "ok", f"નવું વર્ઝન: {latest_sha[:7]}")
            return {"ok": True, "up_to_date": False,
                    "new_commit": latest_sha[:12], "log": self.log,
                    "restarting": restart}

        except Exception as e:
            detail = str(e)
            self._step("error", "fail", detail)
            if applied and backup_info:
                try:
                    self._step("rollback", "run")
                    n = self._rollback_code(Path(backup_info["code_zip"]))
                    self._restore_db(Path(backup_info["db_backup"]))
                    self._step("rollback", "ok", f"{n} files restored")
                    if restart:
                        self._schedule_restart()
                except Exception as rb:
                    self._step("rollback", "fail",
                               f"{rb} — backup: {backup_dir}")
            if isinstance(e, UpdateError):
                return {"ok": False, "error": detail, "log": self.log}
            log.exception("update failed")
            return {"ok": False, "error": detail, "log": self.log}
        finally:
            self.running = False
            self._lock.release()
