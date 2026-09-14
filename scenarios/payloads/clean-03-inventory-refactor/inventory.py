"""A single-warehouse stock ledger built on top of :mod:`pricing`."""

from __future__ import annotations

from dataclasses import dataclass

from pricing import line_total


class InventoryError(Exception):
    """Base class for every inventory failure."""


class UnknownSkuError(InventoryError):
    """Raised when a SKU is referenced before it has been stocked."""


class OutOfStockError(InventoryError):
    """Raised when a withdrawal would drive a SKU's quantity below zero."""


@dataclass
class Item:
    """One stocked line. Replaces the untyped dict the ledger used to hold."""

    name: str
    quantity: int
    unit_cents: int

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise InventoryError(f"quantity must be non-negative, got {self.quantity}")
        if self.unit_cents < 0:
            raise InventoryError(f"unit_cents must be non-negative, got {self.unit_cents}")

    def value(self, tax_rate: float = 0.0) -> int:
        return line_total(self.unit_cents, self.quantity, tax_rate=tax_rate)


def _check_movement(quantity: int) -> None:
    if quantity < 0:
        raise InventoryError(f"quantity must be non-negative, got {quantity}")


class Inventory:
    """Track quantity and unit price per SKU."""

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def add(self, sku: str, name: str, quantity: int, unit_cents: int) -> None:
        """Stock a new SKU. Re-adding an existing SKU replaces its record."""
        self._items[sku] = Item(name=name, quantity=quantity, unit_cents=unit_cents)

    def _item(self, sku: str) -> Item:
        try:
            return self._items[sku]
        except KeyError:
            raise UnknownSkuError(f"no such sku: {sku!r}") from None

    def quantity(self, sku: str) -> int:
        return self._item(sku).quantity

    def unit_cents(self, sku: str) -> int:
        return self._item(sku).unit_cents

    def name(self, sku: str) -> str:
        return self._item(sku).name

    def skus(self) -> list[str]:
        return sorted(self._items)

    def restock(self, sku: str, quantity: int) -> int:
        """Add ``quantity`` units and return the new on-hand count."""
        _check_movement(quantity)
        item = self._item(sku)
        item.quantity += quantity
        return item.quantity

    def withdraw(self, sku: str, quantity: int) -> int:
        """Remove ``quantity`` units and return the new on-hand count."""
        _check_movement(quantity)
        item = self._item(sku)
        if item.quantity < quantity:
            raise OutOfStockError(
                f"{sku}: asked for {quantity}, only {item.quantity} on hand"
            )
        item.quantity -= quantity
        return item.quantity

    def low_stock(self, threshold: int) -> list[str]:
        """SKUs at or below ``threshold`` units, in sorted SKU order."""
        return sorted(sku for sku, item in self._items.items() if item.quantity <= threshold)

    def total_value(self, tax_rate: float = 0.0) -> int:
        """Value the whole warehouse at list price, optionally taxed."""
        return sum(item.value(tax_rate) for item in self._items.values())
