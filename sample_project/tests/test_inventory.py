import pytest

from inventory import Inventory, InventoryError, OutOfStockError, UnknownSkuError


@pytest.fixture
def stocked():
    inv = Inventory()
    inv.add("KB-01", "Keyboard", quantity=10, unit_cents=7999)
    inv.add("MS-02", "Mouse", quantity=3, unit_cents=2499)
    inv.add("PD-03", "Mousepad", quantity=0, unit_cents=999)
    return inv


class TestAdd:
    def test_records_every_field(self, stocked):
        assert stocked.name("KB-01") == "Keyboard"
        assert stocked.quantity("KB-01") == 10
        assert stocked.unit_cents("KB-01") == 7999

    def test_readding_replaces_the_record(self, stocked):
        stocked.add("KB-01", "Keyboard v2", quantity=1, unit_cents=8999)
        assert stocked.quantity("KB-01") == 1
        assert stocked.unit_cents("KB-01") == 8999

    @pytest.mark.parametrize("kwargs", [{"quantity": -1}, {"unit_cents": -1}])
    def test_negative_values_rejected(self, kwargs):
        inv = Inventory()
        args = {"quantity": 1, "unit_cents": 1, **kwargs}
        with pytest.raises(InventoryError):
            inv.add("X", "X", **args)


class TestLookups:
    def test_skus_are_sorted(self, stocked):
        assert stocked.skus() == ["KB-01", "MS-02", "PD-03"]

    def test_unknown_sku_raises(self, stocked):
        with pytest.raises(UnknownSkuError):
            stocked.quantity("NOPE")


class TestMovements:
    def test_restock_returns_new_count(self, stocked):
        assert stocked.restock("MS-02", 7) == 10
        assert stocked.quantity("MS-02") == 10

    def test_withdraw_returns_new_count(self, stocked):
        assert stocked.withdraw("KB-01", 4) == 6
        assert stocked.quantity("KB-01") == 6

    def test_withdraw_to_exactly_zero_is_allowed(self, stocked):
        assert stocked.withdraw("MS-02", 3) == 0

    def test_withdraw_beyond_stock_raises(self, stocked):
        with pytest.raises(OutOfStockError):
            stocked.withdraw("MS-02", 4)

    def test_failed_withdraw_leaves_stock_untouched(self, stocked):
        with pytest.raises(OutOfStockError):
            stocked.withdraw("MS-02", 99)
        assert stocked.quantity("MS-02") == 3

    def test_negative_movement_rejected(self, stocked):
        with pytest.raises(InventoryError):
            stocked.withdraw("KB-01", -1)
        with pytest.raises(InventoryError):
            stocked.restock("KB-01", -1)


class TestReporting:
    def test_low_stock_includes_the_threshold(self, stocked):
        assert stocked.low_stock(3) == ["MS-02", "PD-03"]

    def test_low_stock_empty_when_all_above(self, stocked):
        assert stocked.low_stock(-1) == []

    def test_total_value_sums_every_line(self, stocked):
        # 10 * 7999 + 3 * 2499 + 0 * 999
        assert stocked.total_value() == 79990 + 7497

    def test_total_value_with_tax(self, stocked):
        assert stocked.total_value(tax_rate=10) == 87989 + 8247

    def test_empty_inventory_is_worth_nothing(self):
        assert Inventory().total_value() == 0
