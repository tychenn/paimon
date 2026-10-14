"""Download a pinned official model snapshot in the remote inference environment."""

import argparse
import json
from pathlib import Path

from huggingface_hub import snapshot_download

MODEL_ID = "Qwen/Qwen3.8-27B"
REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    snapshot_download(repo_id=MODEL_ID, revision=REVISION, local_dir=directory, max_workers=4)
    index = json.loads((directory / "model.safetensors.index.json").read_text())
    shards = sorted(set(index["weight_map"].values()))
    missing = [name for name in shards if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(f"Missing model shards: {missing}")
    manifest = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "shards": shards,
        "weight_bytes": sum((directory / name).stat().st_size for name in shards),
    }
    (directory / "volition-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
