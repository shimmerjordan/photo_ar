from photoar import features as F
from photoar import synth
from photoar.bruteforce import BruteForceRecognizer
from photoar.descstore import DescStore, DescStoreWriter


def _build_store(tmp_path, images):
    path = tmp_path / "desc.bin"
    with DescStoreWriter(path, capacity=len(images)) as w:
        for img in images:
            w.append(F.extract(img))
    return DescStore(path)


def test_recognizes_synthetic_query_of_a_known_photo(tmp_path, textured_image):
    images = [textured_image(seed=s, w=1000, h=700) for s in range(5)]
    ids = [f"p{i}" for i in range(5)]
    with _build_store(tmp_path, images) as store:
        rec = BruteForceRecognizer(store, ids)
        query, _ = synth.generate(images[2], count=1, seed=3)[0]
        d = rec.recognize(query)
    assert d.matched
    assert d.photo_id == "p2"


def test_rejects_photo_not_in_library(tmp_path, textured_image):
    images = [textured_image(seed=s) for s in range(5)]
    with _build_store(tmp_path, images) as store:
        rec = BruteForceRecognizer(store, [f"p{i}" for i in range(5)])
        d = rec.recognize(textured_image(seed=12345))
    assert not d.matched


def test_rejects_blank_query(tmp_path, textured_image):
    import numpy as np

    images = [textured_image(seed=s) for s in range(3)]
    with _build_store(tmp_path, images) as store:
        rec = BruteForceRecognizer(store, ["a", "b", "c"])
        d = rec.recognize(np.full((400, 600, 3), 128, np.uint8))
    assert not d.matched


def test_id_count_must_match_store_size(tmp_path, textured_image):
    import pytest

    with _build_store(tmp_path, [textured_image(seed=0)]) as store:
        with pytest.raises(ValueError):
            BruteForceRecognizer(store, ["a", "b"])
