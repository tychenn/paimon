"""Fetch identical official ModelScope shards, verify against a pinned HF manifest.

The two manifests must be downloaded from the official repository APIs first.
Small configuration/tokenizer files always come from the pinned Hugging Face revision.
Partial transfers are resumable; completed files are cryptographically checked.
"""

import argparse
import hashlib
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlencode

from download_model import MODEL_ID, REVISION


def checksum(path, lfs):
    digest = hashlib.sha256() if lfs else hashlib.sha1()
    if not lfs:
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hf-manifest", type=Path, required=True)
    parser.add_argument("--mirror-manifest", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--hf-proxy", default="")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    hf = json.loads(args.hf_manifest.read_text())
    mirror = json.loads(args.mirror_manifest.read_text())
    if hf["sha"] != REVISION or hf["id"] != MODEL_ID:
        raise ValueError("Unexpected Hugging Face model/revision")
    mirror_files = {item["Path"]: item for item in mirror["Data"]["Files"]}
    selected = [
        item
        for item in hf["siblings"]
        if Path(item["rfilename"]).suffix in {".safetensors", ".json", ".jinja", ".txt"}
        or item["rfilename"] == "LICENSE"
    ]
    args.directory.mkdir(parents=True, exist_ok=True)

    def download(item):
        name = item["rfilename"]
        if Path(name).name != name:
            raise ValueError("Unexpected nested model file")
        lfs = item.get("lfs")
        expected = lfs["sha256"] if lfs else item["blobId"]
        destination = args.directory / name
        if destination.exists() and checksum(destination, lfs) == expected:
            print(f"verified existing: {name}", flush=True)
            return name
        temporary = destination.with_name(destination.name + ".part")
        proxy = args.hf_proxy
        if name.endswith(".safetensors"):
            other = mirror_files[name]
            if not lfs or other["Sha256"] != expected or other["Size"] != lfs["size"]:
                raise ValueError(f"Mirror mismatch: {name}")
            query = urlencode({"Revision": other["Revision"], "FilePath": name})
            url = f"https://modelscope.cn/api/v1/models/{MODEL_ID}/repo?{query}"
            proxy = ""  # The official domestic mirror is reachable directly.
        else:
            url = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{quote(name)}"
        command = [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--retry",
            "3",
            "--connect-timeout",
            "30",
            "--continue-at",
            "-",
            "--proxy",
            proxy,
            "--output",
            str(temporary),
            url,
        ]
        subprocess.run(command, check=True)
        if checksum(temporary, lfs) != expected:
            raise ValueError(f"Hash mismatch: {name}; remove its .part file before retrying")
        os.replace(temporary, destination)
        print(f"downloaded and verified: {name}", flush=True)
        return name

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        files = list(pool.map(download, selected))
    manifest = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "verified_files": files,
        "source": "official ModelScope shards matched to pinned Hugging Face hashes",
    }
    (args.directory / "paimon-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("All model files verified.", flush=True)


if __name__ == "__main__":
    main()
