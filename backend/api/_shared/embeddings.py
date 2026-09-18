"""Embedding generation shared by the sync endpoint and the batch backfill
script. Not a Vercel route itself (lives under a leading-underscore
directory, which Vercel's Python builder excludes from routing).
"""
import os
import time

from google import genai
from google.genai import types

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 1536  # one of Gemini's officially supported non-native dims (matches existing schema)
MAX_CHARS = 6000  # model caps input at 2048 tokens; stay well under that
CHUNK_SIZE = 100  # texts per API call -- free-tier rate limits for this model aren't published as a fixed number, so batch conservatively rather than guessing
MAX_RETRIES = 3

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

_TASK_TYPE = {
    "document": "RETRIEVAL_DOCUMENT",
    "query": "RETRIEVAL_QUERY",
    None: "RETRIEVAL_DOCUMENT",
}


def _normalize(vector):
    """Gemini's docs: non-3072 output dimensions require manual L2
    normalization (the model only normalizes its native 3072-dim output)."""
    norm = sum(x * x for x in vector) ** 0.5
    if norm == 0:
        return vector
    return [x / norm for x in vector]


def _embed_chunk(chunk, task_type):
    config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=EMBEDDING_DIM)
    for attempt in range(MAX_RETRIES):
        try:
            result = _client.models.embed_content(model=EMBEDDING_MODEL, contents=chunk, config=config)
            return [_normalize(e.values) for e in result.embeddings]
        except Exception as exc:
            is_rate_limit = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
            if not is_rate_limit or attempt == MAX_RETRIES - 1:
                raise
            time.sleep(5 * (2 ** attempt))  # 5s, 10s, 20s


def embed_texts(texts, input_type="document"):
    """Embed a batch of strings, chunked to stay within a conservative
    per-call size, with retry-on-rate-limit. `input_type` ("document" or
    "query") maps to Gemini's task_type -- it tunes vectors differently for
    storage vs. search-query embedding, improving retrieval quality.
    Returns a list of float vectors in the same order as `texts`."""
    cleaned = [t.strip()[:MAX_CHARS] for t in texts]
    task_type = _TASK_TYPE.get(input_type, "RETRIEVAL_DOCUMENT")

    vectors = []
    for i in range(0, len(cleaned), CHUNK_SIZE):
        vectors.extend(_embed_chunk(cleaned[i:i + CHUNK_SIZE], task_type))
    return vectors
