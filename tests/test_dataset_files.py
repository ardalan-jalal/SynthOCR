from pathlib import Path

import pytest

from synthocr.verify import dataset_files


def test_dataset_walk_is_lazy_and_does_not_call_glob(tmp_path, monkeypatch):
    image = tmp_path / "images/00000000.png"
    image.parent.mkdir()
    image.write_bytes(b"image")
    metadata = tmp_path / "manifest.jsonl"
    metadata.write_text("{}\n")

    def eager_glob(*args, **kwargs):
        pytest.fail("Dataset traversal must stream directory entries")

    monkeypatch.setattr(Path, "glob", eager_glob)
    monkeypatch.setattr(Path, "rglob", eager_glob)
    iterator = dataset_files(tmp_path)
    assert iter(iterator) is iterator
    assert set(iterator) == {image, metadata}


@pytest.mark.parametrize("directory", [False, True])
def test_dataset_walk_rejects_links(tmp_path, directory):
    source = tmp_path / "source"
    if directory:
        source.mkdir()
    else:
        source.write_text("private")
    root = tmp_path / "dataset"
    root.mkdir()
    (root / "link").symlink_to(source, target_is_directory=directory)
    with pytest.raises(ValueError, match="symbolic links"):
        list(dataset_files(root))


def test_dataset_walk_bounds_directory_depth(tmp_path):
    nested = tmp_path
    for _ in range(34):
        nested /= "child"
        nested.mkdir()
    with pytest.raises(ValueError, match="excessive nesting"):
        list(dataset_files(tmp_path))
