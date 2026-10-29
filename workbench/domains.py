import json
from pathlib import Path


def load_packs(root):
    packs = {}
    for path in sorted(Path(root).glob("*/manifest.json")):
        pack = json.loads(path.read_text(encoding="utf-8"))
        if pack["id"] in packs:
            raise ValueError("Duplicate domain pack ID")
        ids = [s["id"] for s in pack["stages"]]
        if len(ids) != len(set(ids)) or not any(s.get("kind") == "approval" for s in pack["stages"]):
            raise ValueError("Domain pack stage IDs must be unique and include a human-review stage")
        packs[pack["id"]] = pack
    return dict(sorted(packs.items(), key=lambda item: (item[1].get("order", 100), item[0])))
