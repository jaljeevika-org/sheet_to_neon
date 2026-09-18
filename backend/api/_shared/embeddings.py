"""Embedding generation shared by the sync endpoint and the batch backfill
script. Not a Vercel route itself (lives under a leading-underscore
directory, which Vercel's Python builder excludes from routing).
"""
import os

import voyageai

EMBEDDING_MODEL = "voyage-4-lite"
EMBEDDING_DIM = 1024
MAX_CHARS = 8000  # guard against pathologically long descriptions

_client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])


def embed_texts(texts, input_type="document"):
    """Embed a batch of strings in a single API call. `input_type` should be
    "document" when embedding rows for storage and "query" when embedding a
    search query — Voyage tunes the vectors differently for each, which
    improves retrieval quality. Returns a list of float vectors in the same
    order as `texts`."""
    cleaned = [t.strip()[:MAX_CHARS] for t in texts]
    result = _client.embed(
        cleaned, model=EMBEDDING_MODEL, input_type=input_type, output_dimension=EMBEDDING_DIM
    )
    return result.embeddings
