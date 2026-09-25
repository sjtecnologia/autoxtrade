import pytest

from integration.market_data_request import write_data_command


def test_write_data_command_payload_ascii_sem_newline(tmp_path):
    path = write_data_command(
        tmp_path,
        command="SUBSCRIBE_SYMBOLS",
        content="EURUSD.pr",
        command_id=10001,
        wait=False,
    )

    assert path.name == "DWX_Commands_0.txt"
    assert path.read_bytes() == b"<:10001|SUBSCRIBE_SYMBOLS|EURUSD.pr:>"


def test_write_data_command_nao_sobrescreve_slot_existente(tmp_path):
    (tmp_path / "DWX_Commands_0.txt").write_bytes(b"existing")

    with pytest.raises(TimeoutError, match="já existe"):
        write_data_command(
            tmp_path,
            command="SUBSCRIBE_SYMBOLS",
            content="EURUSD.pr",
            command_id=10001,
            wait=False,
        )


def test_write_data_command_rejeita_ordem_antes_de_escrever(tmp_path):
    with pytest.raises(AssertionError, match="proibido"):
        write_data_command(
            tmp_path,
            command="OPEN_ORDER",
            content="EURUSD.pr,buy,1",
            command_id=10001,
            wait=False,
        )

    assert not list(tmp_path.iterdir())
