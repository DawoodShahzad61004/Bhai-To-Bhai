---
title: "Install Guide"
---

Installing Maestro-Flow is a two-step process: a global CLI install, then project initialization.

---

## Quick Install

```bash
# 1. Install the global CLI
npm install -g maestro-flow

# 2. Initialize the project (run from the project root)
maestro install
```

**Prerequisites**:
- Node.js ≥ 18
- Claude Code CLI (required)
- Codex CLI / Gemini CLI (optional, for multi-agent workflows)

---

## Installation Flow

`maestro install` performs the following steps:

1. **Detect project state** — empty project / existing code / existing .workflow/
2. **Select components** — interactive component selection screen
3. **Choose the install mode** — global (~/.maestro/) or project-level (.workflow/)
4. **Copy files** — copied to the target location per the component definitions
5. **Generate the manifest** — records the installed components and supports incremental updates

---

## Supported Platforms

In the first step of `maestro install`, you can select the target AI-assisted coding platforms needed for the current project or development environment.

Because there are 60+ supported platforms, the interactive TUI platform selection screen is **paginated** (10 entries per page).
* **Page switching**: use the **Left/Right arrow keys**, the **`h`/`l` keys**, or the **`[`/`]` keys** to turn pages.
* **Shortcut keys**: the first 9 entries on each page are locally labeled `[1]` through `[9]`; press the corresponding number key to toggle that entry.
* **Page indicator**: a pagination indicator is shown at the bottom, e.g. `Page 1 / 7 [● ○ ○ ○ ○ ○ ○]`.

The full list of currently supported platforms is below (it covers most mainstream AI coding clients and agent harnesses):

| Platform ID | Platform Name (Label) | Install Target Path / Description |
|---------|------------------|-------------------|
| `claude` | Claude Code | Core slash commands, skills, agents, hooks, MCP |
| `codex` | Codex | Agents, skills, hooks, MCP |
| `cursor` | Cursor | Skills, agents → copied to `.cursor/` |
| `agy` | Agy (Gemini CLI) | Skills, agents, hooks → copied to `.gemini/` |
| `copilot` | GitHub Copilot | Skills, agents → copied to `.github/` |
| `kiro` | Kiro | Skills, agents → copied to `.kiro/` |
| `opencode` | OpenCode | Skills, agents → copied to `.opencode/` |
| `kilo` | Kilo Code | Skills, agents → copied to `.kilocode/` |
| `devin` | Devin | Skills, agents → copied to `.devin/` |
| `qoder` | Qoder / Qoder CN | Skills, agents → copied to `.qoder/` |
| `codebuddy` | CodeBuddy | Skills, agents → copied to `.codebuddy/` |
| `droid` | Droid | Skills, agents → copied to `.factory/` |
| `trae` | Trae / Trae CN | Skills, agents → copied to `.trae/` |
| `roo` | Roo Code | Skills, agents → copied to `.roo/` |
| `aider-desk` | AiderDesk | Skills, agents → copied to `.aider-desk/` |
| `amp` | Amp | Skills, agents → copied to `.amp/` |
| `antigravity` | Antigravity | Skills, agents → copied to `.antigravity/` |
| `antigravity-cli` | Antigravity CLI | Skills, agents → copied to `.antigravity-cli/` |
| `astrbot` | AstrBot | Skills, agents → copied to `.astrbot/` |
| `autohand-code` | Autohand Code CLI | Skills, agents → copied to `.autohand/` |
| `augment` | Augment | Skills, agents → copied to `.augment/` |
| `bob` | IBM Bob | Skills, agents → copied to `.bob/` |
| `cline` | Cline | Skills, agents → copied to `.cline/` |
| `codearts-agent` | CodeArts Agent | Skills, agents → copied to `.codeartsdoer/` |
| `codemaker` | Codemaker | Skills, agents → copied to `.codemaker/` |
| `codestudio` | Code Studio | Skills, agents → copied to `.codestudio/` |
| `command-code` | Command Code | Skills, agents → copied to `.commandcode/` |
| `continue` | Continue | Skills, agents → copied to `.continue/` |
| `cortex` | Cortex Code | Skills, agents → copied to `.cortex/` |
| `crush` | Crush | Skills, agents → copied to `.crush/` |
| `deepagents` | Deep Agents | Skills, agents → copied to `.deepagents/` |
| `dexto` | Dexto | Skills, agents → copied to `.dexto/` |
| `eve` | Eve | Skills, agents → copied to `agent/` |
| `firebender` | Firebender | Skills, agents → copied to `.firebender/` |
| `forgecode` | ForgeCode | Skills, agents → copied to `.forge/` |
| `goose` | Goose | Skills, agents → copied to `.goose/` |
| `hermes-agent` | Hermes Agent | Skills, agents → copied to `.hermes/` |
| `inference-sh` | inference.sh | Skills, agents → copied to `.inferencesh/` |
| `jazz` | Jazz | Skills, agents → copied to `.jazz/` |
| `junie` | Junie | Skills, agents → copied to `.junie/` |
| `iflow-cli` | iFlow CLI | Skills, agents → copied to `.iflow/` |
| `kimi-code-cli` | Kimi Code CLI | Skills, agents → copied to `.kimi-code-cli/` |
| `kode` | Kode | Skills, agents → copied to `.kode/` |
| `lingma` | Lingma | Skills, agents → copied to `.lingma/` |
| `loaf` | Loaf | Skills, agents → copied to `.loaf/` |
| `mcpjam` | MCPJam | Skills, agents → copied to `.mcpjam/` |
| `mistral-vibe` | Mistral Vibe | Skills, agents → copied to `.vibe/` |
| `moxby` | Moxby | Skills, agents → copied to `.moxby/` |
| `mux` | Mux | Skills, agents → copied to `.mux/` |
| `openhands` | OpenHands | Skills, agents → copied to `.openhands/` |
| `ona` | Ona | Skills, agents → copied to `.ona/` |
| `qwen-code` | Qwen Code | Skills, agents → copied to `.qwen/` |
| `replit` | Replit | Skills, agents → copied to `.replit/` |
| `reasonix` | Reasonix | Skills, agents → copied to `.reasonix/` |
| `rovodev` | Rovo Dev | Skills, agents → copied to `.rovodev/` |
| `tabnine-cli` | Tabnine CLI | Skills, agents → copied to `.tabnine/` |
| `terramind` | Terramind | Skills, agents → copied to `.terramind/` |
| `tinycloud` | Tinycloud | Skills, agents → copied to `.tinycloud/` |
| `warp` | Warp | Skills, agents → copied to `.warp/` |
| `windsurf` | Windsurf | Skills, agents → copied to `.windsurf/` |
| `zed` | Zed | Skills, agents → copied to `.zed/` |
| `zencoder` | Zencoder / Zenflow | Skills, agents → copied to `.zencoder/` |
| `neovate` | Neovate | Skills, agents → copied to `.neovate/` |
| `pochi` | Pochi | Skills, agents → copied to `.pochi/` |
| `promptscript` | PromptScript | Skills, agents → copied to `.promptscript/` |
| `adal` | AdaL | Skills, agents → copied to `.adal/` |
| `agents-standard` | Open Standard | `.agents/` open specification format (cross-platform) |

> **Pi Agent note**: Maestro no longer installs the Pi platform directly (it no longer copies skills/agents into `~/.pi/`).
> Install the official Maestro Flow plugin inside Pi to integrate with the Pi platform:
>
> ```bash
> pi install https://github.com/catlog22/pi-maestro-flow
> ```

---

## Component Groups

Since v0.5.32, the installable components have been consolidated from 53 individual entries into 25 groups, for a simpler selection experience.

### Core Components (selected by default)

| Group | Description | File Count |
|------|------|--------|
| **commands** | Core slash commands | ~30 |
| **hooks** | Automation hooks | ~5 |
| **workflows** | Workflow scripts | ~10 |
| **specs** | Spec templates | 7 |

### Optional Skill Packs

| Group | Included Skills | Description |
|------|----------|------|
| **skills-extra-team** | team-arch-opt, team-brainstorm, team-designer, team-frontend, team-issue, team-planex, and others | Team-collaboration skills |
| **skills-scholar** | scholar-anti-ai-writing, scholar-citation-verify, scholar-experiment, scholar-ideation, and others | Academic research skills |
| **skills-meta** | meta-workflow, meta-analysis, and others | Meta-skills and workflow orchestration |

### Built-in Team Skills (always installed)

The following 9 team skills are installed automatically with the core components and do not need to be selected separately:

- team-adversarial-swarm
- team-coordinate
- team-executor
- team-lifecycle-v4
- team-quality-assurance
- team-review
- team-swarm
- team-tech-debt
- team-testing

---

## Install Modes

### Global Mode (recommended)

Installs into `~/.maestro/`, shared by all projects:

```bash
maestro install --mode global
```

Best for: a personal development machine, sharing configuration across multiple projects

### Project Mode

Installs into the project's `.workflow/` directory, affecting only the current project:

```bash
maestro install --mode project
```

Best for: team collaboration, project-specific configuration

---

## Subcommands

`maestro install` provides the following subcommands for jumping straight to a specific installation step:

| Subcommand | Description |
|--------|------|
| `maestro install components` | Install file components (interactive component selection) |
| `maestro install hooks` | Install hooks (interactive level selection) |
| `maestro install mcp` | Register MCP servers (interactive tool selection) |
| `maestro install toggle` | Enable/disable installed commands, skills, and agents |
| `maestro install fonts` | Install font assets |
| `maestro install wizard` | Launch the full interactive TUI wizard (legacy) |

Every subcommand supports `--global` or `--path <dir>` to specify the installation scope.

---

## Toggle — Enable/Disable Management

`maestro install toggle` offers both an interactive TUI and a non-interactive command line for managing the enabled state of installed commands, skills, and agents.

### Three-State Model

Every entry has one of three states:

| State | Icon | Meaning |
|------|------|------|
| **on** | ✓ | Installed and enabled |
| **off** | ✗ | Installed but disabled (the file is renamed to `.md.disabled`) |
| **available** | · | Present in the source directory but not yet installed to the target location |

Disabling mechanism: the `.md` file is renamed to `.md.disabled`, and renamed back when re-enabled. For skills, disabling renames `SKILL.md` → `SKILL.md.disabled`.

### Interactive TUI

```bash
# Toggle for the global installation
maestro install toggle

# Toggle for a project installation
maestro install toggle --path ./my-project
```

The ToggleView interface has three tabs:

| Tab | Contents |
|--------|------|
| **Commands** | All `.claude/commands/*.md` command files |
| **Skills** | All `.claude/skills/*/SKILL.md` skill directories |
| **Agents** | All `.claude/agents/*.md` agent files |

Controls:
- **Tab** — switch tabs (Shift+Tab to go backwards)
- **Space** — toggle the current entry's state (available→on, on→off, off→on)
- **Up/Down arrows** — move the cursor
- **Enter** — save and exit (updates the `disabledItems` list in the manifest)
- **Escape** — exit (unsaved changes are saved automatically)

Viewport window: when there are more than 20 entries, scroll hints are shown (↑ N more / ↓ N more).

You can restrict the tabs with the `--type` flag:

```bash
# Show only the commands tab
maestro install toggle --type command
```

### Non-Interactive Operations

```bash
# List all entries and their states
maestro install toggle --list

# Filter by type
maestro install toggle --list --type skill

# Enable in bulk
maestro install toggle --enable "maestro-ralph,maestro-search"

# Disable in bulk
maestro install toggle --disable "team-swarm,team-review"
```

---

## Config Profile — Export/Import

An installation configuration can be exported to a JSON profile file, for sharing across a team or reproducing an installation in a CI environment.

### Exporting a Profile

```bash
# Export from the global installation config
maestro install --export

# Export to a specific path
maestro install --export ./team-profile.json

# Export from a project's config
maestro install --path ./my-project --export
```

The exported profile contains the complete installation configuration: component selections, hook level, MCP config, statusline theme, and so on.

### Importing a Profile

```bash
# Non-interactive install from a profile
maestro install --import ./team-profile.json
```

Importing runs the full installation flow automatically, with no manual intervention. Best for:
- Standardizing a team's development environment
- Quickly bootstrapping CI/CD environments
- Syncing configuration across multiple machines

### Profile Storage Location

Exported profiles are saved to the `~/.maestro/install-profiles/` directory by default.

---

## Extra MCP Targets

Beyond Claude Code, `maestro install` can register MCP servers with the following IDEs/tools:

| Target ID | Config File Path | Description |
|---------|-------------|------|
| `cursor` | `.cursor/mcp.json` | Cursor IDE |
| `qoder` | `mcp.json` in the project root | Qoder |
| `trae` | `.mcp.json` | Trae IDE |
| `kiro` | `.kiro/settings/mcp.json` | Kiro IDE |
| `roo` | `.roo/mcp.json` | Roo Code (project-level only) |
| `vscode-copilot` | `.vscode/mcp.json` | VS Code Copilot |
| `gemini-cli` | `.gemini/settings.json` | Gemini CLI |

In the interactive install wizard, the Extra MCP step lets you select which of the targets above to register with. Each target supports both global and project scope.

MCP tool list (6 tools): `write_file`, `edit_file`, `read_file`, `read_many_files`, `team_msg`, `store_knowhow`

---

## Migrating from Older Versions

### Automatic Migration (v0.5.32+)

Individual skill IDs from older versions are mapped automatically to the new group IDs:

| Old ID | New ID |
|--------|-------|
| team-arch-opt | skills-extra-team |
| team-brainstorm | skills-extra-team |
| scholar-ideation | skills-scholar |
| ... | ... |

Migration runs automatically at install time; no manual action is needed.

### Manual Migration

If you need to update manually:

```bash
# Force a reinstall
maestro install --force
```

---

## Updating

```bash
# Check for updates (check only, do not install)
maestro update --check

# Update to the latest version
maestro update

# Preview update notices (used together with --notices)
maestro update --notices --dry-run

# Non-interactive update (CI/automation scenarios)
maestro update --non-interactive
```

### Update Flow

Running `maestro update` automatically performs a three-step flow:

1. **Reinstall the workflows** — using the profile-based mechanism (`manifestToProfile + spawn --import --upgrade`)
2. **Apply version notices** — display the feature/tool/skill changes in the new version
3. **Run migrations** — execute any necessary data migrations

### Profile-Based Reinstall Mechanism

v0.5.37 introduced a profile-based reinstall mechanism that works around the Windows command-line length limit (~8192 characters) and shell escaping problems:

- `manifestToProfile()` exports the current install state to a temporary profile JSON
- `spawn --import --upgrade` re-imports it using the new version
- `mergeNewDefaults()` automatically merges new default components into the existing selection

### The --upgrade Flag

```bash
# Import a profile and merge in the new default components
maestro install --import profile.json --upgrade
```

The `--upgrade` flag tells the install command to call `mergeNewDefaults()` during import, automatically adding components in the new version whose `defaultSelected !== false`.

### Update Options

| Option | Description |
|------|------|
| `--check` | Check for updates only; do not install |
| `--notices` | Display version notices |
| `--dry-run` | Preview changes (must be used with `--notices`) |
| `--from <ver>` | Specify the starting version (notice filtering) |
| `--to <ver>` | Specify the target version (notice filtering) |
| `--non-interactive` | Non-interactive mode (CI/automation) |
| `--migrate <path>` | Run a specific migration script (internal use) |

---

## Uninstalling

```bash
# Interactive uninstall
maestro uninstall

# Bulk uninstall (skip confirmation)
maestro uninstall --yes
```

Uninstalling will:
1. Remove the installed component files
2. Clean up the manifest records
3. Preserve the project data in `.workflow/` (specs, knowhow, etc.)

---

## Network Proxy

To install through a proxy, configure it in `~/.maestro/cli-tools.json`:

```json
{
  "proxy": {
    "enabled": true,
    "httpProxy": "http://127.0.0.1:7890",
    "noProxy": "127.0.0.1,localhost"
  }
}
```

---

## FAQ

### The install hangs

1. Check your network connection
2. Try configuring a proxy (see above)
3. Use `--verbose` to see detailed logs

### Missing components

```bash
# Force a reinstall
maestro install --force
```

### Permission errors

A global install may require administrator privileges:
```bash
# macOS/Linux
sudo npm install -g maestro-flow

# Windows (run as administrator)
npm install -g maestro-flow
```

---

## Related Commands

```bash
# Install management
maestro install [--global] [--path <dir>] [--force]
maestro install [--export [path]] [--import <path>] [--upgrade]
maestro install [--load <path>]  # Load a profile into the interactive TUI
maestro uninstall [--yes]
maestro update [--check] [--notices] [--dry-run] [--from <ver>] [--to <ver>] [--non-interactive]

# Subcommands
maestro install components [--global | --path <dir>]
maestro install hooks [--global | --project]
maestro install mcp [--global | --path <dir>]
maestro install toggle [--global | --path <dir>] [--type <type>] [--enable <names>] [--disable <names>] [--list]
maestro install fonts
maestro install wizard

# Version information
maestro --version
```
