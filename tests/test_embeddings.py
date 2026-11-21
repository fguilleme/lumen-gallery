from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from gallery.embeddings import ClipEmbedder, preprocess_clip


def test_clip_preprocess_uses_official_normalization_and_center_crop():
    image = Image.new('RGB', (448, 224), (255, 0, 0))
    tensor = preprocess_clip(image)
    assert tensor.shape == (3, 224, 224)
    assert tensor.dtype == np.float32
    expected = (np.array([1, 0, 0]) - [0.48145466, 0.4578275, 0.40821073]) / [0.26862954, 0.26130258, 0.27577711]
    np.testing.assert_allclose(tensor[:, 100, 100], expected, rtol=1e-6)


def test_clip_missing_model_fails_locally_without_download(tmp_path):
    embedder = ClipEmbedder(tmp_path / 'absent.onnx')
    with pytest.raises(FileNotFoundError, match='modèle'):
        embedder.embed([tmp_path / 'image.jpg'])


def test_real_local_clip_inference_is_normalized_and_recognizes_same_image(tmp_path):
    model = Path(__file__).resolve().parents[1] / 'data/models/clip-vit-base-patch32/vision_model_quantized.onnx'
    if not model.exists():
        pytest.skip('optional local CLIP model not installed')
    # Real inference on a real RAW-derived fixture JPEG, not a mocked ONNX session.
    from gallery.exif import ExifTool, preview_bytes
    project = Path(__file__).resolve().parents[1]
    tool = ExifTool(project / 'vendor/exiftool/exiftool', 'perl')
    source = project / 'tests/fixtures/RAW_CANON_40D_SRAW_V103.CR2'
    a = tmp_path / 'a.jpg'
    a.write_bytes(preview_bytes(source, tool))
    b = tmp_path / 'b.jpg'
    with Image.open(a) as image:
        image.resize((600, 400)).save(b, quality=75)
    c = tmp_path / 'c.jpg'
    Image.new('RGB', (600, 400), 'blue').save(c)
    values = ClipEmbedder(model).embed([a,b,c])
    assert values.shape == (3, 512)
    assert np.isfinite(values).all()
    np.testing.assert_allclose(np.linalg.norm(values, axis=1), 1, atol=1e-5)
    assert float(values[0] @ values[1]) > 0.90
    assert float(values[0] @ values[1]) > float(values[0] @ values[2]) + .15
