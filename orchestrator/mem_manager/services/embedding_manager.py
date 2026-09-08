import concurrent.futures
import logging
import time

import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from config import EMBEDDING_ENCODING_TIMEOUT_SECONDS, EMBEDDING_MODEL_NAME

logger = logging.getLogger(__name__)


class EmbeddingEncodingTimeoutError(Exception):
    """Raised when model.encode() does not complete within EMBEDDING_ENCODING_TIMEOUT_SECONDS."""
    def __init__(self, model_name: str, timeout: float) -> None:
        self.model_name = model_name
        self.timeout = timeout
        super().__init__(f"model.encode() for '{model_name}' did not complete within {timeout:.0f}s.")


class EmbeddingManager:
    def __init__(self, model_name: str = EMBEDDING_MODEL_NAME):
        self.model_name = model_name
        self.model = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self._load_model()

    def _load_model(self):
        try:
            self.model = SentenceTransformer(self.model_name, device=self.device)
            logger.info(
                f"Model '{self.model_name}' loaded on {self.device}. "
                f"Embedding dimension: {self.model.get_embedding_dimension()}"
            )
            if self.device == "cuda":
                logger.info("GPU: %s", torch.cuda.get_device_name(0))
            else:
                logger.info("CUDA not available; running on CPU.")
        except Exception:
            logger.exception(f"Error loading model '{self.model_name}'")
            raise

    def generate_embedding(self, text, batch_size: int = 32, show_progress=False) -> np.ndarray:
        if self.model is None:
            raise ValueError("Model not loaded.")
        logger.debug(f"  [embedding] calling model.encode() on '{self.model_name}'")
        try:
            t0 = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _executor:
                _future = _executor.submit(
                    self.model.encode,
                    text,
                    batch_size=batch_size,
                    show_progress_bar=show_progress,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    device=self.device,
                )
                try:
                    result = _future.result(timeout=EMBEDDING_ENCODING_TIMEOUT_SECONDS)
                except concurrent.futures.TimeoutError:
                    raise EmbeddingEncodingTimeoutError(self.model_name, EMBEDDING_ENCODING_TIMEOUT_SECONDS)
            logger.debug(
                f"  [embedding] model.encode() on '{self.model_name}' took "
                f"{time.perf_counter() - t0:.3f}s"
            )
            return result
        except EmbeddingEncodingTimeoutError as e:
            logger.error(
                f"  [embedding] model.encode() for '{e.model_name}' did not complete within "
                f"{e.timeout:.0f}s (EMBEDDING_ENCODING_TIMEOUT_SECONDS={EMBEDDING_ENCODING_TIMEOUT_SECONDS})"
                f" — aborting encoding."
            )
            raise
        except Exception:
            logger.exception("Error generating embedding")
            raise

    def get_embedding_dimension(self) -> int:
        if self.model is None:
            raise ValueError("Model not loaded.")
        return self.model.get_embedding_dimension()
    
    def cosine_similarity(self, vec_a: np.ndarray, vec_b: np.ndarray) -> float:
        """Compute cosine similarity between two vectors."""
        if vec_a.shape != vec_b.shape:
            raise ValueError("Vectors must have the same shape.")
        if np.linalg.norm(vec_a) == 0 or np.linalg.norm(vec_b) == 0:
            return 0.0
        return float(np.dot(vec_a, vec_b) / (np.linalg.norm(vec_a) * np.linalg.norm(vec_b)))
