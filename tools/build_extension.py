"""Build a relocatable native Qwen Code extension ZIP using the standard library."""
import argparse
import json
from pathlib import Path
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "flow-steps"


def build(output=None):
    manifest = json.loads((SOURCE / "qwen-extension.json").read_text(encoding="utf-8"))
    if manifest.get("name") != "flow-steps" or manifest.get("skills") != "skills":
        raise ValueError("Expected a flow-steps extension with skills in skills/")
    for name in ("fs-run", "fs-confirm"):
        if not (SOURCE / "skills" / name / "SKILL.md").is_file():
            raise ValueError(f"Missing skill: {name}")
    target = Path(output).resolve() if output else ROOT / "dist" / f'flow-steps-{manifest["version"]}.zip'
    if target.is_relative_to(SOURCE):
        raise ValueError("Place the output archive outside the extension source directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(SOURCE.rglob("*")):
                relative = path.relative_to(SOURCE)
                if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                    continue
                if path.is_symlink():
                    raise ValueError(f"Extension sources must not contain symlinks: {relative}")
                if not path.is_file() or path.suffix in {".pyc", ".pyo"}:
                    continue
                entry = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(build(args.output))
