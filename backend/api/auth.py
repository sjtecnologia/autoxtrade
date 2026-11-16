"""Autenticação via Bearer Token estático."""
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings

bearer_scheme = HTTPBearer(auto_error=False)


def verify_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> str:
    """Valida o token Bearer usando comparação timing-safe.

    Raises 401 se ausente ou inválido.
    """
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), settings.api_secret_token.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido ou ausente.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials
