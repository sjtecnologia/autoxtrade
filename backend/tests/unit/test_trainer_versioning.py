from pathlib import Path


def _read_trainer_source() -> str:
    trainer_path = Path(__file__).resolve().parents[2] / "ml" / "trainer.py"
    return trainer_path.read_text(encoding="utf-8")


def test_make_version_includes_timeframe_in_signature_and_return() -> None:
    source = _read_trainer_source()

    assert "def _make_version(" in source
    assert 'timeframe: str = "1h"' in source
    assert 'tf = timeframe.lower().replace("/", "_")' in source
    assert 'return f"rf_{mkt}_{sym}_{tf}{profile_suffix}_{ts}"' in source


def test_make_version_keeps_robust_suffix_optional() -> None:
    source = _read_trainer_source()

    assert 'profile_suffix = "" if profile in {"", "robust"} else f"_{profile}"' in source
