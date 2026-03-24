"""Long-term memory management using vector embeddings."""

import json
import logging
import math
from typing import Any, Dict, List, Optional

from .storage import OuroborosStorage
from .codebase import get_repo_root

log = logging.getLogger(__name__)


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(v1, v2))
    magnitude1 = math.sqrt(sum(x * x for x in v1))
    magnitude2 = math.sqrt(sum(x * x for x in v2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)


class IndexManager:
    def __init__(self, client: Any, storage: Optional[OuroborosStorage] = None):
        self.client = client
        self.storage = storage or OuroborosStorage()

    def get_embedding(self, text: str, model: str = "text-embedding-3-small") -> List[float]:
        """Generate embedding for text using OpenAI."""
        text = text.replace("\n", " ")
        try:
            return self.client.embeddings.create(input=[text], model=model).data[0].embedding
        except Exception:
            log.exception("Embedding generation failed")
            return []

    def index_file(self, file_path: str, content: str):
        """Index a code file."""
        # Chunking could be more sophisticated (e.g. by function)
        # For now, index the whole file if small, or first 2000 chars
        embedding = self.get_embedding(content[:2000])
        if embedding:
            self.storage.add_embedding("code", file_path, content[:500], embedding)

    def index_failure(self, task_id: str, description: str, failure_msg: str):
        """Index a failed improvement attempt."""
        text = f"Task: {description}\nFailure: {failure_msg}"
        embedding = self.get_embedding(text)
        if embedding:
            self.storage.add_embedding("failure", task_id, text, embedding)

    def retrieve_relevant_context(self, query: str, limit: int = 3) -> str:
        """Search memory for relevant past experiences."""
        query_vec = self.get_embedding(query)
        if not query_vec:
            return ""

        all_embeddings = self.storage.search_embeddings()
        scored = []
        for item in all_embeddings:
            vec = json.loads(item["embedding"])
            score = cosine_similarity(query_vec, vec)
            scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = scored[:limit]
        
        if not results:
            return ""

        parts = ["### Relevant Past Context"]
        for score, item in results:
            if score < 0.3: continue # Threshold
            parts.append(f"- [{item['content_type']}] {item['ref_id']}: {item['content']} (similarity: {score:.2f})")
        
        return "\n".join(parts) if len(parts) > 1 else ""
