"""Requirement tracker CLI: index, allocate, check, bootstrap, sync (REQ-001/002/004).

Run with the dashboard's Python from the dashboard folder, e.g.

    .venv\\Scripts\\python.exe scripts\\review_sync.py index --project app-dashboard
    .venv\\Scripts\\python.exe scripts\\review_sync.py allocate --project my-app --count 3
    .venv\\Scripts\\python.exe scripts\\review_sync.py check
    .venv\\Scripts\\python.exe scripts\\review_sync.py bootstrap --dry-run
    .venv\\Scripts\\python.exe scripts\\review_sync.py sync --project app-dashboard --dry-run

Settings come from the dashboard's `.env.runtime` (`--env-file`): REVIEW_GITHUB_OWNER,
REVIEW_GITHUB_OWNER_TYPE, REVIEW_GITHUB_PROJECT_NUMBER, REVIEW_GITHUB_REPO, REVIEW_GITHUB_APP_FIELD,
REVIEW_PROJECTS_DIR, VEDA_REGISTRY.
The token (`REVIEW_GITHUB_TOKEN`) is read from that file only and never printed.
`index` and `allocate` work offline; the others need the token.
The sync never sets Approved or Changes requested.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SECRET_KEYS = {"REVIEW_GITHUB_TOKEN", "GITHUB_TOKEN"}


def load_settings(env_file: Path) -> None:
    """Copy non-secret settings from the env file into this process (existing env wins)."""
    try:
        lines = env_file.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match or match.group(1) in SECRET_KEYS:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(match.group(1), value)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Thai messages on a cp1252 console
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", default=str(ROOT / ".env.runtime"))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    p_index = sub.add_parser("index", help="record local REQ blocks in the requirements index (offline)")
    p_index.add_argument("--project", required=True)
    p_alloc = sub.add_parser("allocate", help="issue new REQ ids (offline, locked)")
    p_alloc.add_argument("--project", required=True)
    p_alloc.add_argument("--count", type=int, default=1)
    sub.add_parser("check", help="verify token, repo, project and fields")
    p_boot = sub.add_parser("bootstrap", help="check + create missing REQ-ID / App text fields (REVIEW_GITHUB_APP_FIELD)")
    p_boot.add_argument("--dry-run", action="store_true")
    p_sync = sub.add_parser("sync", help="one-way sync requirements.md -> issues/cards")
    p_sync.add_argument("--project", required=True)
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.add_argument("--no-reconcile", action="store_true", help="do not run the requirementsReady reconcile afterwards")
    args = parser.parse_args(argv)

    env_file = Path(args.env_file)
    load_settings(env_file)
    os.environ["REVIEW_ENV_FILE"] = str(env_file)
    os.environ["REVIEW_TRACKER"] = "github"

    from services import app_registry, requirements_index
    from services.review_parser import parse_file
    from services.review_service import ReviewConfig, ReviewService, build_tracker
    from services.review_sync import SyncAbort, SyncPermissionError, Syncer, bootstrap, update_index_offline
    from services.github_projects import build_projects
    from services.review_tracker import TrackerError

    config = ReviewConfig.from_env(Path(app_registry.REGISTRY_PATH))
    projects_dir, index_path = config.projects_dir, config.index_path
    assert index_path is not None and config.github is not None
    log_path = projects_dir.parent / "audit" / "requirement-sync.jsonl"

    def out(data: object, text: str) -> None:
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else text)

    try:
        if args.command == "index":
            written = update_index_offline(projects_dir, index_path, args.project)
            out({"written": written, "index": str(index_path)}, f"index: {len(written)} entries updated in {index_path}")
            return 0
        if args.command == "allocate":
            reserved = []
            for path in projects_dir.glob("*/requirements.md"):
                reserved += [item.req_id for item in parse_file(path, path.parent.name)]
            ids = requirements_index.allocate(index_path, args.project, args.count, reserved=reserved)
            out({"ids": ids}, "\n".join(ids))
            return 0
        ops = build_projects(config.github)
        if args.command == "check":
            viewer = ops.viewer()
            meta = ops.meta()
            ops.repository_id()
            scopes = (viewer.get("headers") or {}).get("x-oauth-scopes")
            result = {"login": viewer.get("login"), "scopes": scopes, "project": meta.title, "missing": meta.missing(),
                      "tokenExpires": (viewer.get("headers") or {}).get("github-authentication-token-expiration")}
            out(result, "\n".join(f"{k}: {v}" for k, v in result.items()))
            return 0 if not result["missing"] else 2
        if args.command == "bootstrap":
            result = bootstrap(ops, dry_run=args.dry_run)
            out(result, json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2
        if args.command == "sync":
            report = Syncer(ops, projects_dir=projects_dir, index_path=index_path, log_path=log_path).run(args.project, dry_run=args.dry_run)
            data = report.to_dict()
            if not args.dry_run and not args.no_reconcile:
                service = ReviewService(config, build_tracker(config))
                data["reconcile"] = service.reconcile(args.project, actor="system:sync")
            lines = [f"{a.req_id}: {a.action} {a.detail} [{a.status_before} -> {a.status_after}]" for a in report.actions]
            lines.append(f"{'(dry-run) ' if args.dry_run else ''}{report.writes} change(s) for {args.project}")
            if "reconcile" in data:
                lines.append(str(data["reconcile"].get("message")))
            out(data, "\n".join(lines))
            return 0
    except TrackerError as exc:
        out({"ok": False, "code": exc.code, "error": exc.message, **({"details": exc.details} if exc.details else {})}, f"error [{exc.code}]: {exc.message}")
        return 3
    except (SyncAbort, SyncPermissionError, requirements_index.IndexError_) as exc:
        out({"ok": False, "error": str(exc)}, f"error: {exc}")
        return 4
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
