"""
Semantic fuzzy lookup for the Text-to-Performance master LUTs.

The ROS nodes stay on Python 3.8, so embedding and vector search live behind
the existing Python 3.11 Logos Chroma sidecar. This module is deliberately a
small HTTP client: animators ask for the closest emoji key, then publish the
matched LUT frames as a normal complete track.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


DEFAULT_SERVER_URL = "http://127.0.0.1:8123"
DEFAULT_COLLECTION = "logos__shared__performance_fuzzy_lut"
DEFAULT_PROVIDER = "model2vec"
DEFAULT_MODEL = "minishlab/potion-base-8M"
DEFAULT_TIMEOUT_S = 2.0


class FuzzyLutError(Exception):
    """The fuzzy LUT sidecar request failed."""


@dataclass
class FuzzyMatch:
    emoji: str
    distance: Optional[float]
    document_id: str
    metadata: Dict[str, Any]
    document: str = ""


class FuzzyLutClient:
    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        collection: str = DEFAULT_COLLECTION,
        provider: str = DEFAULT_PROVIDER,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        n_results: int = 3,
    ):
        self.server_url = server_url.rstrip("/")
        self.collection = collection
        self.provider = provider
        self.model = model
        self.timeout_s = timeout_s
        self.n_results = n_results

    def query(self, text: str, channel: str) -> Optional[FuzzyMatch]:
        text = (text or "").strip()
        if not text:
            return None
        payload = {
            "query_texts": [text],
            "n_results": max(1, int(self.n_results)),
            "where": {"channel": channel},
            "include": ["documents", "metadatas", "distances"],
            "embedding_provider": self.provider,
            "embedding_model": self.model,
        }
        data = self._post("/query", payload)
        ids = (data.get("ids") or [[]])[0] or []
        metas = (data.get("metadatas") or [[]])[0] or []
        distances = (data.get("distances") or [[]])[0] or []
        docs = (data.get("documents") or [[]])[0] or []

        for idx, doc_id in enumerate(ids):
            meta = metas[idx] if idx < len(metas) and metas[idx] else {}
            emoji = (meta.get("emoji") or "").strip()
            if not emoji:
                continue
            distance = distances[idx] if idx < len(distances) else None
            document = docs[idx] if idx < len(docs) and docs[idx] else ""
            return FuzzyMatch(
                emoji=emoji,
                distance=distance,
                document_id=doc_id,
                metadata=meta,
                document=document,
            )
        return None

    def _post(self, suffix: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = "%s/collections/%s%s" % (
            self.server_url,
            urllib.parse.quote(self.collection, safe=""),
            suffix,
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise FuzzyLutError("Chroma fuzzy query failed HTTP %s: %s" %
                                (e.code, detail)) from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise FuzzyLutError("Chroma fuzzy query failed: %s" % e) from e
