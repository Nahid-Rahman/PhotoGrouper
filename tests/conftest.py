import os, sys

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def photo(seed, size=(900, 600)):
    """A smooth, photo-like test image. Different seeds give clearly different pictures."""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, (6, 9, 3), dtype=np.uint8)
    im = Image.fromarray(small).resize(size, Image.BICUBIC)
    noise = rng.normal(0, 3, (size[1], size[0], 3))
    return Image.fromarray(np.clip(np.asarray(im, float) + noise, 0, 255).astype(np.uint8))


@pytest.fixture
def make_photo():
    return photo
