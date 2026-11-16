"""Pedido único de market data DWX com whitelist fail-closed."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

ALLOWED_COMMANDS = frozenset({
    "SUBSCRIBE_SYMBOLS",
    "SUBSCRIBE_SYMBOLS_BAR_DATA",
    "GET_HISTORIC_DATA",
    "GET_HISTORIC_TRADES",
    "RESET_COMMAND_IDS",
})
FORBIDDEN_COMMANDS = frozenset({
    "OPEN_ORDER",
    "CLOSE_ORDER",
    "CLOSE_ALL_ORDERS",
    "CLOSE_ORDERS_BY_SYMBOL",
    "CLOSE_ORDERS_BY_MAGIC",
    "MODIFY_ORDER",
})
COMMAND_NAME = "DWX_Commands_0.txt"


class CommandNotConsumed(TimeoutError):
    pass


def _validate_command(command: str, content: str, command_id: int) -> None:
    if command in FORBIDDEN_COMMANDS:
        raise AssertionError(f"Comando de ordem proibido: {command}")
    if command not in ALLOWED_COMMANDS:
        raise AssertionError(f"Comando fora da whitelist de dados: {command}")
    if command_id <= 0:
        raise ValueError("ID deve ser inteiro positivo")
    try:
        content.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Conteúdo deve ser ASCII/ANSI") from exc
    if any(char in content for char in "\r\n"):
        raise ValueError("Conteúdo não pode conter newline")


def wait_until_consumed(command_path: Path, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while command_path.exists():
        if time.monotonic() >= deadline:
            raise CommandNotConsumed(f"EA não consumiu {command_path} em {timeout_seconds:.0f}s")
        time.sleep(0.25)


def write_data_command(
    directory: str | Path,
    *,
    command: str,
    content: str,
    command_id: int,
    timeout_seconds: float = 60.0,
    wait: bool = True,
) -> Path:
    """Escreve exatamente um comando de dados e aguarda consumo do EA."""
    _validate_command(command, content, command_id)
    root = Path(directory).expanduser()
    command_path = root / COMMAND_NAME
    if command_path.exists():
        raise CommandNotConsumed(
            f"{command_path} já existe; não será sobrescrito antes do consumo pelo EA"
        )
    payload = f"<:{command_id}|{command}|{content}:>"
    encoded = payload.encode("ascii")
    if not encoded.endswith(b":>") or b"\n" in encoded or b"\r" in encoded:
        raise AssertionError("Payload DWX inválido: newline ou terminador ausente")
    command_path.write_bytes(encoded)
    if wait:
        wait_until_consumed(command_path, timeout_seconds)
    return command_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=None, help="Diretório DWX")
    parser.add_argument("--command", required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--id", required=True, type=int, dest="command_id")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    directory = args.dir or os.environ.get("AUTOXTRADE_MT5_FILES_DIR")
    if not directory:
        parser.error("informe --dir ou AUTOXTRADE_MT5_FILES_DIR")
    path = write_data_command(
        directory,
        command=args.command,
        content=args.content,
        command_id=args.command_id,
        timeout_seconds=args.timeout,
    )
    print(f"comando {args.command_id} consumido: {path}")


if __name__ == "__main__":
    main()
