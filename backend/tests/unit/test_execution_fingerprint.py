import shutil
from pathlib import Path

from paper.fingerprint import code_fingerprint, source_paths


def test_qualification_covers_durable_effects_and_schema(tmp_path):
    backend = Path(__file__).resolve().parents[2]
    for rel in source_paths(backend):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(backend / rel, target)
    original = code_fingerprint(tmp_path)
    target = tmp_path / "paper/entry_time.py"
    target.write_bytes(target.read_bytes() + b"\n# changed execution timing\n")
    assert code_fingerprint(tmp_path) != original
    shutil.copyfile(backend / "paper/entry_time.py", target)
    assert code_fingerprint(tmp_path) == original
    (tmp_path / "alembic/versions/999_future_schema.py").write_text("# changed schema\n")
    assert code_fingerprint(tmp_path) != original
