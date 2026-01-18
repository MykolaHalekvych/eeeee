"""
ibkr_resolve_manual_order_v0.py

Цель: безопасно снять "manual/untracked" TWS order с orderId=0.
Подход:
  - Подключаемся к TWS/IB Gateway с clientId=0
  - Делаем reqOpenOrders() -> это bind'ит ручные TWS ордера в client 0 (выдаёт API orderId)
  - Находим ордер по permId и отменяем cancelOrder(orderId)

Важно:
  - Скрипт НИЧЕГО не отменяет без флага --confirm
  - Если bind не дал orderId (остался 0), можно использовать --global-cancel (тоже требует --confirm),
    либо отменить в UI TWS.

Зависимость: ibapi (обычно уже есть вместе с IBKR API). Если нет:
  py -3.11 -m pip install ibapi
"""

from __future__ import annotations

import argparse
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order


TERMINAL_STATUSES = {"Cancelled", "ApiCancelled", "Filled", "Inactive"}


@dataclass
class OpenOrderInfo:
    order_id: int
    perm_id: int
    client_id: int
    account: str
    symbol: str
    sec_type: str
    exchange: str
    action: str
    order_type: str
    tif: str
    total_qty: str  # decimal string in newer APIs
    status: str


class IBApp(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._next_valid_id_event = threading.Event()
        self._open_orders_event = threading.Event()
        self._order_status_lock = threading.Lock()

        self.next_valid_id: Optional[int] = None
        self.open_orders: Dict[int, OpenOrderInfo] = {}  # permId -> info
        self.order_status_by_order_id: Dict[int, str] = {}
        self.errors: List[Tuple[int, int, str]] = []

    # --- callbacks ---
    def nextValidId(self, orderId: int) -> None:
        self.next_valid_id = orderId
        self._next_valid_id_event.set()

    def error(self, reqId: int, errorCode: int, errorString: str) -> None:
        # NOTE: reqId can be -1 / 0 for system messages; keep anyway.
        self.errors.append((reqId, errorCode, errorString))

    def openOrder(
        self, orderId: int, contract: Contract, order: Order, orderState
    ) -> None:
        # orderState.status often has current status
        perm_id = int(getattr(order, "permId", 0) or 0)
        client_id = int(getattr(order, "clientId", 0) or 0)
        account = str(getattr(order, "account", "") or "")
        symbol = str(getattr(contract, "symbol", "") or "")
        sec_type = str(getattr(contract, "secType", "") or "")
        exchange = str(getattr(contract, "exchange", "") or "")
        action = str(getattr(order, "action", "") or "")
        order_type = str(getattr(order, "orderType", "") or "")
        tif = str(getattr(order, "tif", "") or "")

        # totalQuantity can be Decimal-like; safest stringify
        total_qty = str(getattr(order, "totalQuantity", "") or "")

        status = str(getattr(orderState, "status", "") or "")
        info = OpenOrderInfo(
            order_id=int(orderId),
            perm_id=perm_id,
            client_id=client_id,
            account=account,
            symbol=symbol,
            sec_type=sec_type,
            exchange=exchange,
            action=action,
            order_type=order_type,
            tif=tif,
            total_qty=total_qty,
            status=status,
        )
        if perm_id != 0:
            self.open_orders[perm_id] = info

    def openOrderEnd(self) -> None:
        self._open_orders_event.set()

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled,
        remaining,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        with self._order_status_lock:
            self.order_status_by_order_id[int(orderId)] = str(status)

    # Newer APIs can emit this mapping callback after binding
    def orderBound(self, orderId: int, apiClientId: int, apiOrderId: int) -> None:
        # Not strictly needed; openOrder should give us permId/orderId anyway.
        pass

    # --- helper waits ---
    def wait_connected(self, timeout_s: float) -> bool:
        return self._next_valid_id_event.wait(timeout_s)

    def wait_open_orders(self, timeout_s: float) -> bool:
        return self._open_orders_event.wait(timeout_s)


def _safe_call_cancel_order(app: IBApp, order_id: int) -> None:
    # Python ibapi versions differ; try 2-arg first (as in docs), fallback to 1-arg.
    try:
        app.cancelOrder(order_id, "")
    except TypeError:
        app.cancelOrder(order_id)


def _safe_call_global_cancel(app: IBApp) -> None:
    try:
        app.reqGlobalCancel()
    except TypeError:
        # Some bindings may require an OrderCancel object; not handled here.
        raise RuntimeError(
            "reqGlobalCancel() signature mismatch in this ibapi version."
        )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497)
    p.add_argument(
        "--client-id", type=int, default=0, help="Для bind ручных ордеров должен быть 0"
    )
    p.add_argument(
        "--perm-id",
        type=int,
        required=True,
        help="permId ордера (из твоих логов/снапшота)",
    )
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument(
        "--confirm", action="store_true", help="Без этого флага ничего не отменяется"
    )
    p.add_argument(
        "--global-cancel",
        action="store_true",
        help="Отменить ВСЕ открытые ордера (опаснее)",
    )
    args = p.parse_args()

    app = IBApp()
    app.connect(args.host, args.port, args.client_id)
    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    if not app.wait_connected(timeout_s=args.timeout):
        print(
            "[ERR] Не дождались nextValidId. Проверь TWS/Gateway, порт, API settings."
        )
        try:
            app.disconnect()
        except Exception:
            pass
        return 2

    # ВАЖНО: reqOpenOrders() (а не reqAllOpenOrders) bind'ит manual TWS orders для clientId=0
    # и возвращает активные ордера этого clientId. См. IB docs.
    app._open_orders_event.clear()
    app.reqOpenOrders()
    if not app.wait_open_orders(timeout_s=args.timeout):
        print(
            "[WARN] openOrderEnd не пришёл за таймаут; продолжаем с тем, что успели получить."
        )

    target = app.open_orders.get(args.perm_id)
    print(f"[INFO] Найдено open orders (permId->info): {len(app.open_orders)}")
    if target:
        print("[INFO] TARGET ORDER:")
        print(
            f"  permId={target.perm_id} orderId={target.order_id} clientId={target.client_id} "
            f"acct={target.account} {target.symbol} {target.action} {target.order_type} "
            f"tif={target.tif} qty={target.total_qty} status={target.status}"
        )
    else:
        print(
            f"[INFO] Ордер с permId={args.perm_id} не найден среди open orders этого clientId."
        )
        print("       Если он виден в TWS, но не пришёл сюда, попробуй:")
        print("       - убедиться что подключение именно с clientId=0")
        print(
            "       - повторить запуск, либо использовать --global-cancel (с --confirm)"
        )
        try:
            app.disconnect()
        except Exception:
            pass
        return 0

    if not args.confirm:
        print("[SAFE] --confirm не задан. Ничего не отменяю.")
        print("       Для отмены конкретного ордера: добавь --confirm")
        print(
            "       Для глобальной отмены всех ордеров: добавь --global-cancel --confirm"
        )
        try:
            app.disconnect()
        except Exception:
            pass
        return 0

    if args.global_cancel:
        print("[ACTION] reqGlobalCancel() -> отмена ВСЕХ открытых ордеров")
        _safe_call_global_cancel(app)
        # Дадим немного времени на статусы
        time.sleep(2.0)
        print("[INFO] Global cancel sent. Проверь в TWS/verify состояние ордеров.")
        try:
            app.disconnect()
        except Exception:
            pass
        return 0

    if target.order_id == 0:
        print(
            "[ERR] После bind'а orderId всё ещё 0 -> индивидуально отменить через API нельзя."
        )
        print("      Варианты:")
        print("      1) Отмени в TWS UI")
        print(
            "      2) Запусти этот же скрипт с --global-cancel --confirm (отменит ВСЕ open orders)"
        )
        try:
            app.disconnect()
        except Exception:
            pass
        return 3

    print(f"[ACTION] cancelOrder(orderId={target.order_id})")
    _safe_call_cancel_order(app, target.order_id)

    # Попробуем дождаться terminal status по orderStatus (не гарантируется моментально)
    deadline = time.time() + args.timeout
    last_status = None
    while time.time() < deadline:
        with app._order_status_lock:
            last_status = app.order_status_by_order_id.get(target.order_id)
        if last_status in TERMINAL_STATUSES:
            break
        time.sleep(0.2)

    print(f"[INFO] last_status(orderId={target.order_id}) = {last_status}")
    print("[INFO] Дальше проверь через reset_verify_v1 / TWS, что open orders пусты.")
    try:
        app.disconnect()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
