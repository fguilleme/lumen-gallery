"""Offline CLIP image embeddings. No downloads or remote image processing."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import numpy as np
from PIL import Image

MODEL_REPOSITORY = 'Xenova/clip-vit-base-patch32'
MODEL_REVISION = '6b7c523703ae26e39be4b03774966f8571c7f523'
MODEL_SHA256 = '583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299'
MODEL_VERSION = f'clip-vit-base-patch32-int8:{MODEL_SHA256}'
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


def preprocess_clip(image: Image.Image) -> np.ndarray:
    """Official shortest-edge resize, center crop, RGB, scale and normalize."""
    image = image.convert('RGB')
    width, height = image.size
    if width <= height:
        resized = (224, int(224 * height / width))
    else:
        resized = (int(224 * width / height), 224)
    image = image.resize(resized, Image.Resampling.BICUBIC)
    left, top = (image.width - 224) // 2, (image.height - 224) // 2
    image = image.crop((left, top, left + 224, top + 224))
    values = (np.asarray(image, dtype=np.float32) / 255.0 - _MEAN) / _STD
    return np.ascontiguousarray(values.transpose(2, 0, 1))


class ClipEmbedder:
    """Lazily load the pinned, verified quantized model on CPU with four threads."""
    def __init__(self, model_path: Path):
        self.model_path = Path(model_path)
        self._session = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._session is None:
                if not self.model_path.is_file():
                    raise FileNotFoundError('Le modèle local CLIP est absent ; installer le modèle avant l’analyse.')
                with self.model_path.open('rb') as handle:
                    digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                if digest != MODEL_SHA256:
                    raise ValueError('Le modèle local CLIP ne correspond pas à la version vérifiée.')
                import onnxruntime as ort
                options = ort.SessionOptions()
                options.intra_op_num_threads = 4
                options.inter_op_num_threads = 1
                self._session = ort.InferenceSession(str(self.model_path), sess_options=options, providers=['CPUExecutionProvider'])
            return self._session

    def embed(self, paths: list[Path]) -> np.ndarray:
        if not paths:
            return np.empty((0, 512), dtype=np.float32)
        session = self._load()
        tensors = []
        for path in paths:
            with Image.open(path) as image:
                tensors.append(preprocess_clip(image))
        values = np.asarray(session.run(['image_embeds'], {'pixel_values': np.stack(tensors)})[0], dtype=np.float32)
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        if not np.isfinite(values).all() or np.any(norms < 1e-12):
            raise ValueError('CLIP a produit une représentation invalide.')
        return np.asarray(values / norms, dtype=np.float32)
