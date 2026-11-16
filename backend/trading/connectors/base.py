"""Interface abstrata para connectors de exchange."""
from abc import ABC, abstractmethod
from decimal import Decimal

from trading.connectors.types import Balance, OHLCVCandle, Order, OrderSide, OrderType
from trading.safety import AccountNature


class BaseConnector(ABC):
    """Contrato que todos os connectors devem implementar."""

    account_nature = AccountNature.UNKNOWN

    def _bind(self, context=None, transport=None):
        self._context, self._transport = context, transport
        self._closed, self._owner_loop = False, None

    @property
    def context(self):
        return self._context

    @property
    def closed(self):
        return self._closed

    def retire(self):
        self._closed = True

    def _check_use(self, capability):
        import asyncio
        from trading.safety import require_position_management
        from trading.contracts import CapabilityUnavailable
        require_position_management(self)
        if self._closed:
            raise CapabilityUnavailable("Connector is closed")
        loop = asyncio.get_running_loop()
        if self._owner_loop is not None and self._owner_loop is not loop:
            raise CapabilityUnavailable("Connector belongs to another event loop")
        self._owner_loop = loop
        self.context.require(capability)
        if capability not in getattr(self, "supported_capabilities", frozenset()):
            raise CapabilityUnavailable(f"Adapter capability unavailable: {capability.value}")

    async def create_order(self, intent):
        from trading.contracts import CapabilityUnavailable
        raise CapabilityUnavailable("create_order unavailable")

    async def query_order(self, *, intent_id=None, order_id=None):
        from trading.contracts import CapabilityUnavailable
        raise CapabilityUnavailable("Order query unavailable")

    async def list_pending_orders(self):
        from trading.contracts import CapabilityUnavailable
        raise CapabilityUnavailable("Pending order listing unavailable")

    async def list_positions(self):
        from trading.contracts import CapabilityUnavailable
        raise CapabilityUnavailable("Position listing unavailable")

    async def cancel_pending_order(self, order_id, *, intent_id):
        from trading.contracts import CapabilityUnavailable
        raise CapabilityUnavailable("Pending cancellation unavailable")

    async def modify_position_protection(self, position_id, *, stop_loss, take_profit=None, intent_id):
        from trading.contracts import CapabilityUnavailable
        raise CapabilityUnavailable("Position protection unavailable")

    async def reduce_position(self, position_id, quantity, *, intent_id):
        from trading.contracts import CapabilityUnavailable
        raise CapabilityUnavailable("Position reduction unavailable")

    async def get_fills(self, *, order_id=None):
        from trading.contracts import CapabilityUnavailable
        raise CapabilityUnavailable("Fill query unavailable")

    @abstractmethod
    async def connect(self) -> None:
        """Testa a conectividade com a exchange."""

    @abstractmethod
    async def get_balance(self) -> dict[str, Balance]:
        """Retorna saldo disponível por ativo."""

    @abstractmethod
    async def get_ohlcv(
        self, symbol: str, timeframe: str = "5m", limit: int = 200
    ) -> list[OHLCVCandle]:
        """Busca candles OHLCV históricos."""

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: Decimal,
        price: Decimal | None = None,
        params: dict | None = None,
    ) -> Order:
        """Envia uma ordem à exchange."""

    @abstractmethod
    async def place_oco_order(
        self,
        symbol: str,
        side: OrderSide,
        amount: Decimal,
        take_profit_price: Decimal,
        stop_price: Decimal,
        stop_limit_price: Decimal,
    ) -> Order:
        """Envia uma ordem OCO (One-Cancels-Other)."""

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancela uma ordem aberta."""

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Lista ordens abertas."""

    @abstractmethod
    async def get_current_price(self, symbol: str) -> Decimal:
        """Retorna o último preço negociado do símbolo."""

    @abstractmethod
    async def close(self) -> None:
        """Fecha conexões abertas."""
