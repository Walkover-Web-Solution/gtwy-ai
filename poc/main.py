#!/usr/bin/env python3
"""
POC CLI — plan & execute via GTWY, state saved to local JSON files.

Usage:
  python -m poc.main                    # start a new session
  python -m poc.main --resume <id>      # resume an existing session
  python -m poc.main --list             # list all sessions
"""
import argparse
import sys

import poc.state_manager as sm
from poc.config import PLANNER_BRIDGE_ID, EXECUTOR_BRIDGE_ID
from poc.planner import run_planning
from poc.executor import run_execution


def _check_config() -> bool:
    missing = []
    if not PLANNER_BRIDGE_ID:
        missing.append("PLANNER_BRIDGE_ID")
    if not EXECUTOR_BRIDGE_ID:
        missing.append("EXECUTOR_BRIDGE_ID")
    if missing:
        print(f"[error] Missing env vars: {', '.join(missing)}")
        print("        Set them or edit poc/config.py before running.")
        return False
    return True


def cmd_list() -> None:
    sessions = sm.list_sessions()
    if not sessions:
        print("No sessions found.")
        return
    for sid in sessions:
        try:
            s = sm.load_session(sid)
            task_count = len(s.get("plan", []))
            done = sum(1 for t in s.get("plan", []) if t.get("status") == "completed")
            print(f"  {sid}  [{s['status']}]  tasks: {done}/{task_count}  goal: {s['goal'][:60]}")
        except Exception:
            print(f"  {sid}  [unreadable]")


def cmd_new() -> None:
    if not _check_config():
        sys.exit(1)

    print("=" * 60)
    print("  GTWY POC — Planner + Executor CLI")
    print("=" * 60)
    goal = input("\nWhat do you want to accomplish?\n> ").strip()
    if not goal:
        print("No goal provided. Exiting.")
        sys.exit(0)

    session = sm.create_session(goal)
    print(f"\n[session] ID: {session['id']}")

    tasks = run_planning(session)

    print("\n--- Plan ---")
    for t in tasks:
        print(f"  {t['id']}. {t['title']}")
        print(f"     {t['description']}")
    print()

    confirm = input("Proceed with execution? [Y/n] ").strip().lower()
    if confirm in ("n", "no"):
        print("Execution cancelled. Session saved — resume with:")
        print(f"  python -m poc.main --resume {session['id']}")
        sys.exit(0)

    run_execution(session)


def cmd_resume(session_id: str) -> None:
    if not _check_config():
        sys.exit(1)
    try:
        session = sm.load_session(session_id)
    except FileNotFoundError:
        print(f"[error] Session '{session_id}' not found.")
        sys.exit(1)

    print(f"[session] Resuming {session_id}  status={session['status']}")
    if session["status"] == "done":
        print("Session already completed.")
        return
    if session["status"] == "planning":
        tasks = run_planning(session)
        for t in tasks:
            print(f"  {t['id']}. {t['title']}")
        confirm = input("\nProceed with execution? [Y/n] ").strip().lower()
        if confirm in ("n", "no"):
            sys.exit(0)

    run_execution(session)


def main() -> None:
    parser = argparse.ArgumentParser(description="GTWY POC CLI")
    parser.add_argument("--list", action="store_true", help="List all sessions")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Resume a saved session")
    args = parser.parse_args()

    if args.list:
        cmd_list()
    elif args.resume:
        cmd_resume(args.resume)
    else:
        cmd_new()


if __name__ == "__main__":
    main()
