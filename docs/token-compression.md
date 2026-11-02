# Token Compression (Optional)

Moved from `CLAUDE.md` (issue #1338). Neither tool is required for SOVA to function.

## RTK Token Compression (Optional)

SOVA supports RTK (Rust Token Killer) for reducing token consumption by 60-90% on bash command outputs. When RTK is installed, `sova install` automatically configures it.

### Installation

```bash
# Install RTK via Homebrew (macOS/Linux)
brew install rtk

# Verify installation
rtk --version
```

### Manual Setup

If you installed RTK after running `sova install`, add the hook manually to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}
    ]
  }
}
```

**Important**: The command must be `"rtk hook claude"`, not just `"rtk"`. The bare `rtk` command prints help and blocks tool calls. Keep the `matcher`/`hooks` wrapper, or Claude Code ignores the whole file (`sova install --update` repairs it).

### Configuration

RTK is enabled by default. Disable hook injection during `sova install` with:

```bash
sova config set rtk.enabled false
```

### Verification

```bash
# Check if RTK is available
sova doctor

# View RTK savings after a few sessions
rtk gain
```

## Headroom Token Compression (Optional)

Headroom is a broader context-compression tool (JSON, code, logs, prose) than RTK, and is separate from the in-process Headroom compression wired into `sova/llm/` for LLM invocations; see epic #894 for that integration. This section covers the standalone CLI for local use: SOVA's in-process path installs the `compression` extra declared in `pyproject.toml` (`headroom-ai>=0.3.0`), while local wrap/proxy usage below installs the full `headroom-ai[all]` bundle instead.

### Headroom Installation

```bash
pip install "headroom-ai[all]"

# Verify installation
headroom --version
```

### Wrap Mode

Run Claude Code through Headroom without a separate process to manage:

```bash
headroom wrap claude
```

### Proxy Mode

Start a local compressing proxy:

```bash
headroom proxy --port 8787
```

Starting the proxy alone does not route traffic: point Claude Code at it by setting `ANTHROPIC_BASE_URL=http://localhost:8787` in the shell that runs `claude`.

### Headroom Verification

```bash
# View durable compression savings (tokens, cost, per-model breakdown)
headroom savings
```

Headroom is optional and licensed under Apache 2.0. `sova doctor` does not check for it, and installing it is not required for SOVA to function.
