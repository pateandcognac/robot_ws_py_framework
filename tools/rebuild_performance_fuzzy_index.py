#!/usr/bin/env python3
"""
Rebuild the shared fuzzy Text-to-Performance LUT index in Logos Chroma.

The index stores model2vec embeddings for master LUT entries plus optional
Gemini phrase examples that resemble real robot utterances. Runtime ROS nodes
query this collection when a policy cascade includes "fuzzy".
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Tuple


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "src", "logos_hardware", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from performance_lib import fuzzy_lut, luts  # noqa: E402


DEFAULT_PHRASE_DIR = "/home/robot/src/ft_gemma_face/data/augmented/gemini_phrases"


def semantic_slug(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    for prefix in ("emoji_face_seq_", "emoji_arm_seq_"):
        if stem.startswith(prefix):
            return stem[len(prefix):]
    return stem


def load_semantic_entries(dirpath: str) -> Dict[str, Dict[str, Any]]:
    by_slug: Dict[str, Dict[str, Any]] = {}
    for name in sorted(os.listdir(dirpath)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(dirpath, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("emoji") and entry.get("frames"):
                item = dict(entry)
                item["_source_path"] = path
                by_slug[semantic_slug(path)] = item
    return by_slug


def frame_beats(entry: Dict[str, Any]) -> str:
    beats = []
    for frame in entry.get("frames") or []:
        if isinstance(frame, dict) and frame.get("beat"):
            beats.append(str(frame["beat"]))
    return " ".join(beats)


def lut_document(entry: Dict[str, Any]) -> str:
    parts = [
        entry.get("emoji", ""),
        entry.get("name", ""),
        entry.get("ideation", ""),
        frame_beats(entry),
    ]
    return "\n".join(str(p).strip() for p in parts if str(p).strip())


def phrase_document(phrase: str, entry: Dict[str, Any]) -> str:
    parts = [phrase, entry.get("emoji", ""), entry.get("name", "")]
    return "\n".join(str(p).strip() for p in parts if str(p).strip())


def stable_id(channel: str, source_kind: str, slug: str, text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return "performance-fuzzy:%s:%s:%s:%s" % (channel, source_kind, slug, digest)


def make_record(
    channel: str,
    source_kind: str,
    slug: str,
    document: str,
    entry: Dict[str, Any],
    source_path: str,
) -> Dict[str, Any]:
    return {
        "id": stable_id(channel, source_kind, slug, document),
        "document": document,
        "metadata": {
            "channel": channel,
            "emoji": entry.get("emoji", ""),
            "name": entry.get("name", ""),
            "source_kind": source_kind,
            "source_path": source_path,
        },
    }


def build_records(
    face_dir: str = luts.DEFAULT_FACE_SEMANTIC_DIR,
    arm_dir: str = luts.DEFAULT_ARM_SEMANTIC_DIR,
    phrase_dir: str = DEFAULT_PHRASE_DIR,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    channels = {
        "face": load_semantic_entries(face_dir),
        "arms": load_semantic_entries(arm_dir),
    }
    by_emoji = {
        channel: {entry.get("emoji"): entry for entry in entries.values()
                  if entry.get("emoji")}
        for channel, entries in channels.items()
    }

    for channel, entries in channels.items():
        for slug, entry in sorted(entries.items()):
            document = lut_document(entry)
            if document:
                records.append(make_record(
                    channel, "lut_entry", slug, document, entry,
                    entry.get("_source_path", ""),
                ))

    if os.path.isdir(phrase_dir):
        for name in sorted(os.listdir(phrase_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(phrase_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    phrases = json.load(f)
            except Exception:
                continue
            if not isinstance(phrases, list):
                continue
            slug = semantic_slug(path)
            source_entry = channels["face"].get(slug) or channels["arms"].get(slug)
            source_emoji = source_entry.get("emoji") if source_entry else ""
            for channel, entries in channels.items():
                entry = entries.get(slug) or by_emoji[channel].get(source_emoji)
                if not entry:
                    continue
                for phrase in phrases:
                    if not isinstance(phrase, str) or not phrase.strip():
                        continue
                    document = phrase_document(phrase.strip(), entry)
                    records.append(make_record(
                        channel, "gemini_phrase", slug, document, entry, path))

    return records


def post_json(server_url: str, path: str, payload: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    url = server_url.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def delete_collection(server_url: str, collection: str, timeout_s: float) -> None:
    url = "%s/collections/%s" % (
        server_url.rstrip("/"),
        urllib.parse.quote(collection, safe=""),
    )
    req = urllib.request.Request(url, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=timeout_s).close()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def chunks(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for idx in range(0, len(items), size):
        yield items[idx:idx + size]


def upsert_records(
    records: List[Dict[str, Any]],
    server_url: str,
    collection: str,
    provider: str,
    model: str,
    batch_size: int,
    timeout_s: float,
) -> int:
    post_json(server_url, "/collections/get-or-create", {
        "name": collection,
        "metadata": {
            "collection_kind": "performance_fuzzy_lut",
            "embedding_provider": provider,
            "embedding_model": model,
        },
    }, timeout_s)
    total = 0
    for batch in chunks(records, batch_size):
        path = "/collections/%s/upsert" % urllib.parse.quote(collection, safe="")
        post_json(server_url, path, {
            "ids": [r["id"] for r in batch],
            "documents": [r["document"] for r in batch],
            "metadatas": [r["metadata"] for r in batch],
            "embedding_provider": provider,
            "embedding_model": model,
        }, timeout_s)
        total += len(batch)
        print("indexed %d/%d" % (total, len(records)))
    return total


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild the Chroma-backed fuzzy LUT index for Logos TTP.")
    parser.add_argument("--server-url", default=fuzzy_lut.DEFAULT_SERVER_URL)
    parser.add_argument("--collection", default=fuzzy_lut.DEFAULT_COLLECTION)
    parser.add_argument("--provider", default=fuzzy_lut.DEFAULT_PROVIDER)
    parser.add_argument("--model", default=fuzzy_lut.DEFAULT_MODEL)
    parser.add_argument("--face-dir", default=luts.DEFAULT_FACE_SEMANTIC_DIR)
    parser.add_argument("--arm-dir", default=luts.DEFAULT_ARM_SEMANTIC_DIR)
    parser.add_argument("--phrase-dir", default=DEFAULT_PHRASE_DIR)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--reset", action="store_true",
                        help="delete the collection before rebuilding")
    parser.add_argument("--dry-run", action="store_true",
                        help="build records and print counts without contacting Chroma")
    return parser.parse_args(argv)


def summarize(records: List[Dict[str, Any]]) -> Tuple[int, Dict[Tuple[str, str], int]]:
    counts: Dict[Tuple[str, str], int] = {}
    for rec in records:
        meta = rec["metadata"]
        key = (meta.get("channel", ""), meta.get("source_kind", ""))
        counts[key] = counts.get(key, 0) + 1
    return len(records), counts


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    records = build_records(args.face_dir, args.arm_dir, args.phrase_dir)
    total, counts = summarize(records)
    print("built %d fuzzy LUT records" % total)
    for (channel, source_kind), count in sorted(counts.items()):
        print("  %s %-13s %d" % (channel, source_kind, count))
    if args.dry_run:
        return 0
    if args.reset:
        delete_collection(args.server_url, args.collection, args.timeout_s)
    upsert_records(
        records,
        args.server_url,
        args.collection,
        args.provider,
        args.model,
        max(1, args.batch_size),
        args.timeout_s,
    )
    print("done: %s" % args.collection)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
