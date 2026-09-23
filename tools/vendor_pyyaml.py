"""Development-only: vendor a pinned pure-Python PyYAML release from PyPI."""
import hashlib
import argparse
import io
import json
from pathlib import Path
import tarfile
import urllib.request


VERSION = "6.0.3"
DEST = Path(__file__).resolve().parents[1] / "flow-steps/skills/fs-run/scripts/vendor"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    metadata_url = f"https://pypi.org/pypi/PyYAML/{VERSION}/json"
    if args.metadata:
        metadata = json.loads(args.metadata.read_text(encoding="utf-8-sig"))
    else:
        with urllib.request.urlopen(metadata_url, timeout=60) as response:
            metadata = json.load(response)
    source = next(item for item in metadata["urls"] if item["packagetype"] == "sdist")
    if args.archive:
        archive = args.archive.read_bytes()
    else:
        with urllib.request.urlopen(source["url"], timeout=60) as response:
            archive = response.read()
    checksum = hashlib.sha256(archive).hexdigest()
    if checksum != source["digests"]["sha256"]:
        raise RuntimeError("Source archive checksum mismatch")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/")
            relative = None
            if len(parts) == 4 and parts[1:3] == ["lib", "yaml"] and parts[3].endswith(".py"):
                relative = Path("yaml") / parts[3]
            elif len(parts) == 2 and parts[1] == "LICENSE":
                relative = Path("PyYAML.LICENSE")
            if relative is None or not member.isfile():
                continue
            data = tar.extractfile(member).read()
            target = DEST / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    if "yaml/__init__.py" not in files or "PyYAML.LICENSE" not in files:
        raise RuntimeError("Source archive does not contain the expected package")
    provenance = {"name": "PyYAML", "version": VERSION, "source": source["url"],
                  "sha256": checksum, "files": files,
                  "implementation": "Unmodified lib/yaml Python sources; no compiled extension."}
    (DEST / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"Vendored PyYAML {VERSION}: {len(files)} files")


if __name__ == "__main__":
    main()
