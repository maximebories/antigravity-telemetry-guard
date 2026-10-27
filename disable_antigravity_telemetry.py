#!/usr/bin/env python3
"""Disable documented Antigravity / Gemini CLI product-telemetry switches.

Only writes keys the products actually read:

* Antigravity 2.0 (Electron): ~/.gemini/config/config.json
  userSettings.enableTelemetry / showFeedbackSurvey
* Antigravity CLI (agy): ~/.gemini/antigravity-cli/settings.json
  enableTelemetry / showFeedbackSurvey
* Gemini CLI: ~/.gemini/settings.json
  privacy.usageStatisticsEnabled (usage stats; default on)
  telemetry.enabled is OTEL (default off) — pinned off only if already on
* Antigravity IDE (VS Code fork), if that product tree already exists:
  telemetry.telemetryLevel = off, argv.json enable-crash-reporter = false

Also manages the macOS launchd guard (`local.agy-telemetry-guard`) watching both
the Antigravity 2.0 GUI (`~/.gemini/config/config.json` and `~/.gemini/config`)
and the Antigravity CLI (`~/.gemini/antigravity-cli/settings.json`), plus
shared Gemini settings (`~/.gemini/settings.json`).

Does not: create phantom IDE profiles, touch ~/.zshrc (that would also opt
Claude Code out), scrape language-server CSRF, or claim ZDR / no inference
upload. Prompts still leave the machine for hosted models. GUI source of
truth remains Settings → Account → Enable Telemetry.

Usage:
  ./disable_antigravity_telemetry.py
  ./disable_antigravity_telemetry.py --dry-run
  ./disable_antigravity_telemetry.py --verify
  ./disable_antigravity_telemetry.py --install-guard
  ./disable_antigravity_telemetry.py --guard-status
  ./disable_antigravity_telemetry.py --uninstall-guard
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
GUARD_LABEL = "local.agy-telemetry-guard"
GUARD_TEMPLATE_PATH = SCRIPT_DIR / f"{GUARD_LABEL}.plist"
GUARD_PLIST_PATH = HOME / "Library" / "LaunchAgents" / f"{GUARD_LABEL}.plist"
GUARD_LOG_PATH = HOME / ".gemini" / "telemetry-guard.log"


def display_path(path: Path | str) -> str:
    """Format paths relative to cwd (`./...`) or HOME (`~/...`)."""
    p = Path(path)
    try:
        rel_cwd = p.relative_to(Path.cwd())
        return "." if str(rel_cwd) == "." else f"./{rel_cwd}"
    except ValueError:
        pass
    try:
        rel_home = p.relative_to(HOME)
        return "~" if str(rel_home) == "." else f"~/{rel_home}"
    except ValueError:
        return str(p)


def resolve_template_path(raw: str, base_dir: Path) -> str:
    """Expand `./...` relative to base_dir and `~/...` relative to HOME."""
    if raw.startswith("./") or raw.startswith("../"):
        return str((base_dir / raw).resolve())
    if raw.startswith("~"):
        return str(Path(raw).expanduser())
    return raw


# --- JSONC (VS Code settings / argv) ---------------------------------------


def strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments and trailing commas; strings stay intact."""
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = False
    escape = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue
        out.append(ch)
        i += 1
    stripped = "".join(out)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    return stripped


def parse_json_or_jsonc(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(strip_jsonc(text))


_SCALAR = r'(?:true|false|null|"[^"\\]*(?:\\.[^"\\]*)*"|\d+(?:\.\d+)?)'


def jsonc_set_scalar(text: str, key: str, value: Any) -> str:
    """Replace or insert a top-level scalar key, preserving comments."""
    dumped = json.dumps(value)
    key_pat = re.escape(key)
    pattern = re.compile(rf'("{key_pat}"\s*:\s*)({_SCALAR})')
    match = pattern.search(text)
    if match:
        return text[: match.start(2)] + dumped + text[match.end(2) :]

    last = text.rfind("}")
    if last < 0:
        raise ValueError("JSONC object has no closing brace")
    before = text[:last].rstrip()
    stripped_before = strip_jsonc(before).rstrip()
    if stripped_before.endswith("{"):
        insert = f'\n  "{key}": {dumped}\n'
    elif stripped_before.endswith(","):
        insert = f'\n  "{key}": {dumped}\n'
    else:
        insert = f',\n  "{key}": {dumped}\n'
    return before + insert + text[last:]


def _self_test() -> None:
    commented = '{\n  // c\n  "a": 1, // x\n  "b": "http://x"\n}'
    assert parse_json_or_jsonc(commented) == {"a": 1, "b": "http://x"}
    trailing = '{"a": 1,}'
    assert parse_json_or_jsonc(trailing) == {"a": 1}
    block = '{"a": 1 /* z */, "b": false}'
    assert parse_json_or_jsonc(block) == {"a": 1, "b": False}

    src = '{\n  // keep\n  "enable-crash-reporter": true\n}\n'
    out = jsonc_set_scalar(src, "enable-crash-reporter", False)
    assert parse_json_or_jsonc(out)["enable-crash-reporter"] is False
    assert "// keep" in out

    src2 = '{\n  "locale": "en"\n}\n'
    out2 = jsonc_set_scalar(src2, "enable-crash-reporter", False)
    data = parse_json_or_jsonc(out2)
    assert data["enable-crash-reporter"] is False
    assert data["locale"] == "en"

    src3 = '{\n  "telemetry.telemetryLevel": "all"\n}\n'
    out3 = jsonc_set_scalar(src3, "telemetry.telemetryLevel", "off")
    assert parse_json_or_jsonc(out3)["telemetry.telemetryLevel"] == "off"


# --- JSON object helpers ----------------------------------------------------


def nested_dict(root: dict, *keys: str) -> dict:
    cur: Any = root
    path = []
    for k in keys:
        path.append(k)
        val = cur.get(k)
        if val is None:
            nxt: dict[str, Any] = {}
            cur[k] = nxt
            cur = nxt
        elif isinstance(val, dict):
            cur = val
        else:
            joined = ".".join(path)
            raise TypeError(f"{joined} is {type(val).__name__}, not an object")
    return cur


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --- Apply / verify results -------------------------------------------------


@dataclass
class Result:
    name: str
    path: Path
    status: str
    detail: str


def backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.privacy-bak.{stamp}")
    shutil.copy2(path, dest)
    return dest


def write_text(path: Path, text: str, dry_run: bool) -> None:
    if dry_run:
        return
    if path.is_file():
        backup(path)
    path.write_text(text, encoding="utf-8")


def patch_json_file(
    name: str,
    path: Path,
    *,
    required_parent: Path | None,
    create_if_missing: bool,
    mutate: Callable[[dict], bool],
    verify: Callable[[dict], str | None],
    dry_run: bool,
) -> Result:
    if required_parent is not None and not required_parent.is_dir():
        return Result(
            name, path, "skipped", f"no product tree {display_path(required_parent)}"
        )
    if not path.is_file():
        if not create_if_missing:
            return Result(name, path, "skipped", "file not present; will not create")
        data: dict[str, Any] = {}
        mutate(data)
        text = dump_json(data)
        if dry_run:
            return Result(name, path, "applied", "dry-run would create file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        err = verify(loaded)
        if err:
            return Result(name, path, "failed", f"created but verify failed: {err}")
        return Result(name, path, "applied", "created")

    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Result(name, path, "failed", f"invalid JSON ({exc}); refusing to rewrite")
    if not isinstance(data, dict):
        return Result(name, path, "failed", f"root is {type(data).__name__}, not object")

    try:
        changed = mutate(data)
    except TypeError as exc:
        return Result(name, path, "failed", str(exc))

    if not changed:
        err = verify(data)
        if err:
            return Result(name, path, "failed", f"unchanged but verify failed: {err}")
        return Result(name, path, "already", "keys already set")

    text = dump_json(data)
    write_text(path, text, dry_run)
    if dry_run:
        return Result(name, path, "applied", "dry-run would rewrite")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Result(name, path, "failed", f"post-write parse failed: {exc}")
    err = verify(loaded)
    if err:
        return Result(name, path, "failed", f"wrote but verify failed: {err}")
    return Result(name, path, "applied", "updated and verified")


def patch_jsonc_scalars(
    name: str,
    path: Path,
    *,
    required_parent: Path | None,
    updates: dict[str, Any],
    dry_run: bool,
) -> Result:
    if required_parent is not None and not required_parent.is_dir():
        return Result(
            name, path, "skipped", f"no product tree {display_path(required_parent)}"
        )
    if not path.is_file():
        return Result(name, path, "skipped", "file not present; will not create")

    raw = path.read_text(encoding="utf-8")
    try:
        current = parse_json_or_jsonc(raw)
    except json.JSONDecodeError as exc:
        return Result(name, path, "failed", f"invalid JSONC ({exc}); refusing to rewrite")
    if not isinstance(current, dict):
        return Result(name, path, "failed", "root is not an object")

    missing = [k for k, v in updates.items() if current.get(k) != v]
    if not missing:
        return Result(name, path, "already", "keys already set")

    text = raw
    try:
        for key, value in updates.items():
            text = jsonc_set_scalar(text, key, value)
        parse_json_or_jsonc(text)
    except (ValueError, json.JSONDecodeError) as exc:
        return Result(name, path, "failed", f"patch failed: {exc}")

    write_text(path, text, dry_run)
    if dry_run:
        return Result(name, path, "applied", "dry-run would patch JSONC")
    try:
        loaded = parse_json_or_jsonc(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return Result(name, path, "failed", f"post-write parse failed: {exc}")
    bad = [k for k, v in updates.items() if loaded.get(k) != v]
    if bad:
        return Result(name, path, "failed", f"wrote but still wrong: {bad}")
    return Result(name, path, "applied", "updated and verified")


# --- Product targets --------------------------------------------------------


def mutate_antigravity_gui(data: dict) -> bool:
    us = nested_dict(data, "userSettings")
    changed = False
    if us.get("enableTelemetry") is not False:
        us["enableTelemetry"] = False
        changed = True
    if us.get("showFeedbackSurvey") is not False:
        us["showFeedbackSurvey"] = False
        changed = True
    return changed


def verify_antigravity_gui(data: dict) -> str | None:
    us = data.get("userSettings")
    if not isinstance(us, dict):
        return "userSettings missing"
    if us.get("enableTelemetry") is not False:
        return "userSettings.enableTelemetry is not false"
    if us.get("showFeedbackSurvey") is not False:
        return "userSettings.showFeedbackSurvey is not false"
    return None


def mutate_antigravity_cli(data: dict) -> bool:
    changed = False
    if data.get("enableTelemetry") is not False:
        data["enableTelemetry"] = False
        changed = True
    if data.get("showFeedbackSurvey") is not False:
        data["showFeedbackSurvey"] = False
        changed = True
    return changed


def verify_antigravity_cli(data: dict) -> str | None:
    if data.get("enableTelemetry") is not False:
        return "enableTelemetry is not false"
    if data.get("showFeedbackSurvey") is not False:
        return "showFeedbackSurvey is not false"
    return None


def mutate_gemini_cli(data: dict) -> bool:
    changed = False
    privacy = nested_dict(data, "privacy")
    if privacy.get("usageStatisticsEnabled") is not False:
        privacy["usageStatisticsEnabled"] = False
        changed = True
    telemetry = data.get("telemetry")
    if isinstance(telemetry, dict) and telemetry.get("enabled") is True:
        telemetry["enabled"] = False
        telemetry["logPrompts"] = False
        changed = True
    return changed


def verify_gemini_cli(data: dict) -> str | None:
    privacy = data.get("privacy")
    if not isinstance(privacy, dict):
        return "privacy missing"
    if privacy.get("usageStatisticsEnabled") is not False:
        return "privacy.usageStatisticsEnabled is not false"
    telemetry = data.get("telemetry")
    if isinstance(telemetry, dict) and telemetry.get("enabled") is True:
        return "telemetry.enabled is still true (OTEL)"
    return None


def apply_all(*, dry_run: bool, verify_only: bool) -> list[Result]:
    gemini_home = HOME / ".gemini"
    gui_config = gemini_home / "config" / "config.json"
    cli_dir = gemini_home / "antigravity-cli"
    cli_settings = cli_dir / "settings.json"
    gemini_settings = gemini_home / "settings.json"

    gui_installed = (
        gui_config.parent.is_dir()
        or (gemini_home / "antigravity").is_dir()
        or Path("/Applications/Antigravity.app").is_dir()
    )
    cli_installed = (
        cli_dir.is_dir()
        or (HOME / ".local" / "bin" / "agy").is_file()
        or shutil.which("agy") is not None
    )

    ide_support = HOME / "Library" / "Application Support" / "Antigravity IDE"
    ide_settings = ide_support / "User" / "settings.json"
    argv_candidates = [
        HOME / ".antigravity-ide" / "argv.json",
        ide_support / "argv.json",
    ]

    write = not verify_only
    dry = dry_run or verify_only

    results: list[Result] = []
    results.append(
        patch_json_file(
            "Antigravity 2.0 GUI",
            gui_config,
            required_parent=gemini_home if gui_installed else gui_config.parent,
            create_if_missing=gui_installed,
            mutate=mutate_antigravity_gui,
            verify=verify_antigravity_gui,
            dry_run=dry if write else True,
        )
    )
    results.append(
        patch_json_file(
            "Antigravity CLI (agy)",
            cli_settings,
            required_parent=gemini_home if cli_installed else cli_dir,
            create_if_missing=cli_installed,
            mutate=mutate_antigravity_cli,
            verify=verify_antigravity_cli,
            dry_run=dry if write else True,
        )
    )
    results.append(
        patch_json_file(
            "Gemini CLI usage statistics",
            gemini_settings,
            required_parent=gemini_home,
            create_if_missing=True,
            mutate=mutate_gemini_cli,
            verify=verify_gemini_cli,
            dry_run=dry if write else True,
        )
    )

    if ide_support.is_dir():
        results.append(
            patch_jsonc_scalars(
                "Antigravity IDE settings",
                ide_settings,
                required_parent=ide_support / "User",
                updates={"telemetry.telemetryLevel": "off"},
                dry_run=dry if write else True,
            )
        )
        argv_hit = False
        for argv in argv_candidates:
            if argv.is_file():
                results.append(
                    patch_jsonc_scalars(
                        "Antigravity IDE argv",
                        argv,
                        required_parent=argv.parent,
                        updates={"enable-crash-reporter": False},
                        dry_run=dry if write else True,
                    )
                )
                argv_hit = True
                break
        if not argv_hit:
            results.append(
                Result(
                    "Antigravity IDE argv",
                    argv_candidates[0],
                    "skipped",
                    "IDE present but argv.json not found; will not create",
                )
            )
    else:
        results.append(
            Result(
                "Antigravity IDE",
                ide_support,
                "skipped",
                "VS Code-based IDE not installed (2.0 Electron app uses config.json)",
            )
        )

    if verify_only:
        remapped: list[Result] = []
        for r in results:
            if r.status == "applied":
                remapped.append(Result(r.name, r.path, "needed", r.detail))
            else:
                remapped.append(r)
        return remapped
    return results


# --- launchd Guard (GUI + CLI) ----------------------------------------------


def build_guard_plist(script_path: Path) -> dict[str, Any]:
    base_dir = script_path.parent
    if GUARD_TEMPLATE_PATH.is_file():
        data = plistlib.loads(GUARD_TEMPLATE_PATH.read_bytes())
        data["ProgramArguments"] = [
            resolve_template_path(arg, base_dir)
            for arg in data.get("ProgramArguments", [])
        ]
        data["WatchPaths"] = [
            resolve_template_path(wp, base_dir)
            for wp in data.get("WatchPaths", [])
        ]
        if "StandardOutPath" in data:
            data["StandardOutPath"] = resolve_template_path(
                data["StandardOutPath"], base_dir
            )
        if "StandardErrorPath" in data:
            data["StandardErrorPath"] = resolve_template_path(
                data["StandardErrorPath"], base_dir
            )
        return data

    gemini_home = HOME / ".gemini"
    watch_paths = [
        str(gemini_home / "config" / "config.json"),
        str(gemini_home / "config"),
        str(gemini_home / "antigravity-cli" / "settings.json"),
        str(gemini_home / "settings.json"),
    ]
    ide_settings = (
        HOME
        / "Library"
        / "Application Support"
        / "Antigravity IDE"
        / "User"
        / "settings.json"
    )
    if ide_settings.parent.is_dir():
        watch_paths.append(str(ide_settings))

    return {
        "Label": GUARD_LABEL,
        "ProgramArguments": ["/usr/bin/python3", str(script_path)],
        "RunAtLoad": True,
        "ThrottleInterval": 5,
        "WatchPaths": watch_paths,
        "StandardOutPath": str(GUARD_LOG_PATH),
        "StandardErrorPath": str(GUARD_LOG_PATH),
    }


def install_guard(*, dry_run: bool) -> int:
    script_path = Path(__file__).resolve()
    plist_data = build_guard_plist(script_path)
    plist_bytes = plistlib.dumps(plist_data, sort_keys=False)

    print(f"Installing launchd guard: {GUARD_LABEL}")
    print(f"  Script:      {display_path(script_path)}")
    print(f"  Agent plist: {display_path(GUARD_PLIST_PATH)}")
    print(f"  Log file:    {display_path(GUARD_LOG_PATH)}")
    print("  WatchPaths:")
    for wp in plist_data["WatchPaths"]:
        print(f"    - {display_path(wp)}")

    if dry_run:
        print("\n[dry-run] Would apply telemetry switches, write plist, and bootstrap.")
        return 0

    results = apply_all(dry_run=False, verify_only=False)
    print_report(results, dry_run=False, verify_only=False)

    GUARD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUARD_PLIST_PATH.write_bytes(plist_bytes)

    uid = os.getuid()
    domain = f"gui/{uid}"
    service = f"{domain}/{GUARD_LABEL}"

    subprocess.run(
        ["/bin/launchctl", "enable", service],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["/bin/launchctl", "bootout", service],
        check=False,
        capture_output=True,
        text=True,
    )
    boot = subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(GUARD_PLIST_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    if boot.returncode != 0:
        print(f"launchctl bootstrap failed: {boot.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"\nGuard installed and active in {service}.")
    return 0


def uninstall_guard(*, dry_run: bool) -> int:
    uid = os.getuid()
    service = f"gui/{uid}/{GUARD_LABEL}"
    if dry_run:
        print(
            f"[dry-run] Would bootout {service} and remove {display_path(GUARD_PLIST_PATH)}"
        )
        return 0

    subprocess.run(
        ["/bin/launchctl", "bootout", service],
        check=False,
        capture_output=True,
        text=True,
    )
    if GUARD_PLIST_PATH.is_file():
        GUARD_PLIST_PATH.unlink()
        print(f"Removed {display_path(GUARD_PLIST_PATH)} and unloaded {service}.")
    else:
        print(f"{display_path(GUARD_PLIST_PATH)} not present; unloaded {service} if running.")
    return 0


def guard_status() -> int:
    uid = os.getuid()
    service = f"gui/{uid}/{GUARD_LABEL}"
    print(
        f"Guard plist: {display_path(GUARD_PLIST_PATH)} ({'present' if GUARD_PLIST_PATH.is_file() else 'missing'})"
    )
    res = subprocess.run(
        ["/bin/launchctl", "print", service],
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"Launchd status: NOT LOADED ({service})")
        return 1
    print(f"Launchd status: LOADED ({service})")
    for line in res.stdout.splitlines():
        if any(k in line for k in ("state =", "runs =", "last exit code =")):
            print(f"  {line.strip()}")
    return 0


def print_report(results: list[Result], *, dry_run: bool, verify_only: bool) -> int:
    mode = "verify" if verify_only else ("dry-run" if dry_run else "apply")
    print(f"Antigravity / Gemini CLI telemetry ({mode})")
    print()
    failed = 0
    for r in results:
        print(f"  [{r.status:8}] {r.name}")
        print(f"             {display_path(r.path)}")
        print(f"             {r.detail}")
        if r.status == "failed":
            failed += 1
    print()
    print("Prompts still leave this Mac for hosted inference.")
    print("GUI source of truth: Antigravity Settings → Account → Enable Telemetry.")
    print("Does not write ~/.zshrc (DISABLE_TELEMETRY would also opt Claude Code out).")
    print("Does not delete local conversations under ~/.gemini/antigravity*/.")
    if failed:
        print(f"Failed: {failed} target(s).", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change; write nothing",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check current files; write nothing",
    )
    parser.add_argument(
        "--install-guard",
        action="store_true",
        help="install and bootstrap the launchd telemetry guard for GUI and CLI",
    )
    parser.add_argument(
        "--uninstall-guard",
        action="store_true",
        help="unload and remove the launchd telemetry guard",
    )
    parser.add_argument(
        "--guard-status",
        action="store_true",
        help="check whether the launchd telemetry guard is loaded",
    )
    args = parser.parse_args()
    try:
        _self_test()
    except AssertionError as exc:
        print(f"internal self-test failed: {exc}", file=sys.stderr)
        return 2

    if args.guard_status:
        return guard_status()
    if args.uninstall_guard:
        return uninstall_guard(dry_run=args.dry_run)
    if args.install_guard:
        return install_guard(dry_run=args.dry_run)

    results = apply_all(dry_run=args.dry_run, verify_only=args.verify)
    return print_report(results, dry_run=args.dry_run, verify_only=args.verify)


if __name__ == "__main__":
    sys.exit(main())
