"""Settings router — runtime configuration management."""

from fastapi import APIRouter
from pydantic import BaseModel

from app import config

router = APIRouter(tags=["settings"])


class ConfigUpdate(BaseModel):
    key: str
    value: str


class InvariantToggle(BaseModel):
    name: str
    enabled: bool


@router.get("/settings/config")
async def get_config():
    """Get current configuration as key-value pairs."""
    conf_file = config.AGENT_CONF
    conf = {}
    if conf_file.exists():
        for line in conf_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                conf[key.strip()] = val.strip().strip('"').strip("'")
    return {"config": conf, "path": str(conf_file)}


@router.post("/settings/config")
async def update_config(update: ConfigUpdate):
    """Update a single configuration value."""
    conf_file = config.AGENT_CONF
    if not conf_file.exists():
        return {"error": "Config file not found. Run setup first."}

    content = conf_file.read_text()
    lines = content.splitlines()
    updated = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{update.key}="):
            lines[i] = f'{update.key}="{update.value}"'
            updated = True
            break

    if not updated:
        lines.append(f'{update.key}="{update.value}"')

    conf_file.write_text("\n".join(lines) + "\n")
    return {"status": "ok", "key": update.key, "value": update.value}


@router.get("/settings/invariants")
async def list_invariants():
    """List available invariants and their status."""
    invariants_dir = config.DATA_DIR.parent / "invariants"
    if not invariants_dir.exists():
        import os

        pak_root = os.environ.get("PAK_ROOT", "")
        if pak_root:
            invariants_dir = config.DATA_DIR.parent.parent / "invariants"

    result = []
    if invariants_dir.exists():
        for f in sorted(invariants_dir.glob("*.sh")):
            result.append(
                {
                    "name": f.stem,
                    "path": str(f),
                    "enabled": True,
                }
            )

    return {"invariants": result}


@router.get("/settings/personas")
async def list_personas():
    """List available personas."""
    personas = []
    personas_dir = config.SCRIPTS_DIR / "personas"
    if personas_dir.exists():
        for f in sorted(personas_dir.glob("*.md")):
            personas.append(
                {
                    "name": f.stem,
                    "path": str(f),
                    "has_mcp": (f.parent / f"{f.stem}.mcp.json").exists(),
                }
            )

    return {"personas": personas}
