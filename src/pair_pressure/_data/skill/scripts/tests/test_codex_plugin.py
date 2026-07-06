import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[6]


def test_codex_manifest_packages_shared_skill_and_mcp_server():
    manifest = json.loads(
        (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    version = (
        ROOT / "src" / "pair_pressure" / "_data" / "skill" / "VERSION"
    ).read_text(encoding="utf-8").strip()

    assert manifest["name"] == "pair-pressure"
    assert manifest["version"] == version
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"]["pair-pressure"]["command"] == "pair-pressure-mcp"
    assert manifest["mcpServers"]["pair-pressure"]["env"] == {
        "PAIR_PRESSURE_ALIAS": "Codex"
    }
    skill_path = ROOT / "skills" / "pair-pressure" / "SKILL.md"
    assert skill_path.is_file()
    assert "allowed-tools:" not in skill_path.read_text(encoding="utf-8")

    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["pair-pressure"]["command"] == "pair-pressure-mcp"
