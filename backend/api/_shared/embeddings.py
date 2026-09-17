"""Embedding generation shared by the sync endpoint and the batch backfill
script. Not a Vercel route itself (lives under a leading-underscore
directory, which Vercel's Python builder excludes from routing).
"""
import os

from openai import OpenAI

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
MAX_CHARS = 8000  # guard against pathologically long descriptions

_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def embed_texts(texts):
    """Embed a batch of strings in a single API call. Returns a list of
    float vectors in the same order as `texts`."""
    cleaned = [t.strip()[:MAX_CHARS] for t in texts]
    response = _client.embeddings.create(model=EMBEDDING_MODEL, input=cleaned)
    return [d.embedding for d in response.data]
