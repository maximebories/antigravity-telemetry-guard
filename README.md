# Antigravity & `agy` CLI Telemetry Disabler + Guard

Self-contained tooling and instructions to disable all documented client-side telemetry, usage analytics, crash reporting, and feedback surveys across **Google Antigravity 2.0 (Electron GUI)**, **Antigravity CLI (`agy`)**, **Gemini CLI (`gemini`)**, and **Antigravity IDE** (when installed), plus a `launchd` guard daemon covering both GUI and CLI configuration drift.

## Quick Start

```bash
# 1. Preview changes without writing
./disable_antigravity_telemetry.py --dry-run

# 2. Apply telemetry opt-outs across GUI, CLI, and Gemini
./disable_antigravity_telemetry.py

# 3. Verify all targets report [already] or [skipped]
./disable_antigravity_telemetry.py --verify

# 4. Install & bootstrap the launchd guard (GUI + CLI)
./disable_antigravity_telemetry.py --install-guard

# 5. Check guard status
./disable_antigravity_telemetry.py --guard-status
```

---

## 1. Documented Client Switches

Three Google surfaces share `~/.gemini/` but use **separate telemetry keys**:

| Surface | Config File | Keys Written | Notes |
| :--- | :--- | :--- | :--- |
| **Antigravity 2.0 (GUI)** | `~/.gemini/config/config.json` | `userSettings.enableTelemetry: false`<br>`userSettings.showFeedbackSurvey: false` | Electron desktop app (`com.google.antigravity`). GUI toggle lives at **Settings → Account → Enable Telemetry**. |
| **Antigravity CLI (`agy`)** | `~/.gemini/antigravity-cli/settings.json` | `enableTelemetry: false`<br>`showFeedbackSurvey: false` | Uses sparse persistence: unset `enableTelemetry` defaults to **`true`** (usage metrics + crash reports). |
| **Gemini CLI (`gemini`)** | `~/.gemini/settings.json` | `privacy.usageStatisticsEnabled: false` | Preserves existing keys (such as `hooks`). Pins `telemetry.enabled: false` and `telemetry.logPrompts: false` if OpenTelemetry was enabled. |
| **Antigravity IDE** | `~/Library/Application Support/Antigravity IDE/User/settings.json` & `argv.json` | `telemetry.telemetryLevel: "off"`<br>`enable-crash-reporter: false` | Patched **only** if the standalone VS Code-based IDE directory already exists; never creates phantom profiles. |

### Safety Guarantees
- **Parse-fail closed:** Refuses to overwrite any malformed JSON/JSONC file.
- **Automatic backups:** Writes `<file>.privacy-bak.<UTC>` beside any modified config before updating.
- **No `~/.zshrc` pollution:** Never sets global `DISABLE_TELEMETRY` or `DO_NOT_TRACK` in shell startup files (which GUI apps ignore and which cause unintended side effects in other CLIs like Claude Code).
- **No transcript deletion:** Does not delete local conversation databases under `~/.gemini/antigravity/conversations/` or `~/.gemini/antigravity-cli/conversations/`.

---

## 2. Automated `launchd` Guard (GUI + CLI)

Both Antigravity 2.0 (GUI) and `agy` (CLI) use sparse configuration persistence and rewrite their configuration files when settings or workspace trust lists change, which can strip `"enableTelemetry": false` and silently re-enable telemetry.

The `launchd` agent [`local.agy-telemetry-guard.plist`](local.agy-telemetry-guard.plist) monitors **both** the GUI and CLI configuration paths and automatically restores the opt-out keys within seconds:

- **Watched paths (`WatchPaths`):**
  - `~/.gemini/config/config.json` (Antigravity 2.0 GUI settings file)
  - `~/.gemini/config` (Antigravity 2.0 GUI config directory, catching atomic file replacements)
  - `~/.gemini/antigravity-cli/settings.json` (`agy` CLI settings file)
  - `~/.gemini/settings.json` (Shared Gemini CLI settings file)
- **Startup enforcement (`RunAtLoad: true`):** Immediately verifies and enforces all opt-outs on macOS login or service bootstrap.
- **Logs:** `~/.gemini/telemetry-guard.log`

### Install via Script (Recommended)

Reads [`./local.agy-telemetry-guard.plist`](local.agy-telemetry-guard.plist), expands `./` and `~/` for `launchd`, writes `~/Library/LaunchAgents/local.agy-telemetry-guard.plist`, and bootstraps the service:

```bash
./disable_antigravity_telemetry.py --install-guard
```

### Manual Install

Since `launchd` property lists do not expand `./` or `~` at runtime, substitute paths when copying `./local.agy-telemetry-guard.plist` manually:

```bash
sed -e "s|\./|$(pwd)/|g" -e "s|~|$HOME|g" \
    ./local.agy-telemetry-guard.plist > ~/Library/LaunchAgents/local.agy-telemetry-guard.plist

launchctl enable gui/$(id -u)/local.agy-telemetry-guard
launchctl bootout gui/$(id -u)/local.agy-telemetry-guard 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.agy-telemetry-guard.plist
```

### Verify & Uninstall Guard

```bash
# Check launchd service state and run count
./disable_antigravity_telemetry.py --guard-status

# Unload and remove the launchd plist
./disable_antigravity_telemetry.py --uninstall-guard
```

---

## 3. Non-Client Layers (Model Training, Local Transcripts, ZDR)

Client telemetry flags only control Layer 1 (product analytics, crash reports, and client interaction logs).

1. **Hosted Inference & Model Training (Account Level):**
   - Prompts and completions still leave this Mac when using hosted Gemini/Antigravity models.
   - Confirm **Settings → Account → Enable Telemetry** is off in the Antigravity 2.0 GUI.
   - Model training terms follow your authentication contract: unpaid AI Studio / consumer quota may use data for product improvement, whereas paid Cloud-billed Gemini API / Vertex AI / Code Assist Standard & Enterprise do not train on customer data without permission.
2. **Local Transcripts (Disk Storage):**
   - Antigravity 2.0 GUI: `~/.gemini/antigravity/conversations/` (plus `~/.gemini/antigravity/installation_id`)
   - Antigravity CLI (`agy`): `~/.gemini/antigravity-cli/conversations/` and `~/.gemini/antigravity-cli/history.jsonl` (plus `~/.gemini/antigravity-cli/installation_id`)
   - Gemini CLI: `~/.gemini/tmp/<project_hash>/chats/`
3. **Zero Data Retention (ZDR):**
   - ZDR is an organization/project-level contract control (e.g., approved Google Cloud / Gemini Developer API project ZDR), never a local JSON toggle.
