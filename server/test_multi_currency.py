"""RF2 Multi-Currency Retention Engine - tests.

Validates per D10 signed off (cross-sect market + sect shares):
- Multi-currency earn/spend (spirit_stone, fateum, destium, souldust)
- Daily currency sinks (retention pacing)
- Cross-sect exchange (D10)
- Sect shares (D10)
- Dao Insight tracking (RF10)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest
from multi_currency import (
    MultiCurrencyAccount, earn_currency, spend_currency,
    drain_daily_sinks, cross_sect_exchange, buy_sect_share,
    get_sect_share, dao_insight_summary, CURRENCY_LIST, CURRENCY_SINKS,
)


class TestMultiCurrencyEarnSpend:
    """RF2a - Currency earning and spending."""

    def setup_method(self):
        self.account = MultiCurrencyAccount(player_id="p1")
        earn_currency(self.account, "spirit_stone", 1000)

    def test_earn_spirit_stones(self):
        r = earn_currency(self.account, "spirit_stone", 100)
        assert r["ok"]
        assert r["earned"] == 100
        assert self.account.get_balance("spirit_stone") == 1100

    def test_earn_fateum(self):
        r = earn_currency(self.account, "fateum", 50)
        assert r["ok"]
        assert r["earned"] == 50
        assert self.account.get_balance("fateum") == 50

    def test_earn_destium(self):
        r = earn_currency(self.account, "destium", 30)
        assert r["ok"]
        assert r["earned"] == 30
        assert self.account.get_balance("destium") == 30

    def test_earn_souldust(self):
        r = earn_currency(self.account, "souldust", 20)
        assert r["ok"]
        assert r["earned"] == 20
        assert self.account.get_balance("souldust") == 20

    def test_spend_insufficient(self):
        r = spend_currency(self.account, "fateum", 100)
        assert not r["ok"]
        assert "insufficient" in r["error"]

    def test_spend_success(self):
        r = spend_currency(self.account, "spirit_stone", 200)
        assert r["ok"]
        assert r["spent"] == 200
        assert self.account.get_balance("spirit_stone") == 800

    def test_spend_unknown_currency(self):
        r = spend_currency(self.account, "gold", 10)
        assert not r["ok"]
        assert "unknown" in r["error"].lower()

    def test_earn_invalid_amount(self):
        r = earn_currency(self.account, "spirit_stone", 0)
        assert not r["ok"]
        r = earn_currency(self.account, "spirit_stone", -10)
        assert not r["ok"]


class TestCurrencySinks:
    """RF2 - Daily retention sinks."""

    def setup_method(self):
        self.account = MultiCurrencyAccount(player_id="p2")
        earn_currency(self.account, "souldust", 100)
        earn_currency(self.account, "fateum", 50)
        earn_currency(self.account, "spirit_stone", 200)

    def test_daily_sink_drains(self):
        r = drain_daily_sinks(self.account, day=1, sink_id="cultivation_maintenance")
        assert r["ok"]
        assert not r["skipped"]
        assert r["drained"] == {"souldust": 5}
        assert self.account.get_balance("souldust") == 95

    def test_daily_sink_idempotent(self):
        drain_daily_sinks(self.account, day=1, sink_id="artifact_repair")
        r = drain_daily_sinks(self.account, day=1, sink_id="artifact_repair")
        assert r["ok"]
        assert r["skipped"]  # already drained today

    def test_sink_insufficient_balance(self):
        self.account.set_balance("souldust", 0)
        r = drain_daily_sinks(self.account, day=1, sink_id="cultivation_maintenance")
        assert not r["ok"]
        assert "insufficient souldust" in r["error"]
        assert r["drained"] == {}
        assert r["skipped"] is False
        # The period must stay unclaimed so the player can pay once they earn it.
        assert "cultivation_maintenance" not in self.account.daily_claimed

    def test_all_sinks_registered(self):
        """All 4 sinks must be defined (D10 anchoring)."""
        assert len(CURRENCY_SINKS) == 4
        for sink_id in CURRENCY_SINKS:
            assert "drain" in CURRENCY_SINKS[sink_id]


class TestMultiLegSinkAtomicity:
    """A multi-currency sink must charge all legs or none.

    Every shipped sink in CURRENCY_SINKS drains exactly ONE currency, so the
    loop's multi-currency path was latent: present in code, never executed, and
    never asserted on. These tests inject a two-currency sink so that path is
    actually driven, which is the only way a one-currency table can be made to
    prove anything about it.
    """

    DUAL_SINK = {
        "name": "Dual Sink",
        "description": "charges spirit_stone then fateum",
        "drain": {"spirit_stone": 20, "fateum": 2},
        "interval_sec": 86400,
    }

    def setup_method(self):
        self.account = MultiCurrencyAccount(player_id="p_dual")
        earn_currency(self.account, "spirit_stone", 200)
        earn_currency(self.account, "fateum", 1)  # SHORT: the sink needs 2
        CURRENCY_SINKS["_test_dual_sink"] = dict(self.DUAL_SINK)

    def teardown_method(self):
        CURRENCY_SINKS.pop("_test_dual_sink", None)

    def test_short_second_leg_does_not_keep_the_first(self):
        """The pinned loss: enough spirit_stone, not enough fateum -> refund."""
        r = drain_daily_sinks(self.account, day=1, sink_id="_test_dual_sink")

        assert not r["ok"]
        assert "insufficient fateum" in r["error"]
        assert r["drained"] == {}
        # The first leg was debited during the attempt and handed back.
        assert r["refunded"] == {"spirit_stone": 20}
        # Balances are exactly what they were before the call.
        assert self.account.get_balance("spirit_stone") == 200
        assert self.account.get_balance("fateum") == 1
        assert r["remaining"]["spirit_stone"] == 200

    def test_failed_drain_does_not_burn_the_period(self):
        """A refusal must leave the period claimable, otherwise the sink is a
        silent charge the player can never pay off."""
        drain_daily_sinks(self.account, day=1, sink_id="_test_dual_sink")
        assert "_test_dual_sink" not in self.account.daily_claimed

        # After earning the short currency, the same day still pays.
        earn_currency(self.account, "fateum", 5)
        r = drain_daily_sinks(self.account, day=1, sink_id="_test_dual_sink")
        assert r["ok"]
        assert r["drained"] == {"spirit_stone": 20, "fateum": 2}
        assert self.account.get_balance("spirit_stone") == 180
        assert self.account.get_balance("fateum") == 3

    def test_fully_funded_multi_leg_sink_still_takes_both(self):
        """The control: the fix must not break the success path."""
        earn_currency(self.account, "fateum", 1)  # now exactly 2
        r = drain_daily_sinks(self.account, day=1, sink_id="_test_dual_sink")
        assert r["ok"]
        assert r["drained"] == {"spirit_stone": 20, "fateum": 2}
        assert "refunded" not in r
        assert self.account.get_balance("spirit_stone") == 180
        assert self.account.get_balance("fateum") == 0

    def test_short_first_leg_debits_nothing_at_all(self):
        """A shortfall on the FIRST leg must not refund a refund, which would
        mint currency that was never earned."""
        self.account.set_balance("spirit_stone", 1)
        before = dict(self.account.balances)
        r = drain_daily_sinks(self.account, day=1, sink_id="_test_dual_sink")
        assert not r["ok"]
        assert r["refunded"] == {}
        assert dict(self.account.balances) == before


class TestCrossSectExchange:
    """RF2 / D10 - Cross-sect market exchange."""

    def setup_method(self):
        self.account = MultiCurrencyAccount(player_id="p3")
        earn_currency(self.account, "spirit_stone", 500)

    def test_exchange_success(self):
        r = cross_sect_exchange(self.account, "spirit_stone", "fateum", 100)
        assert r["ok"]
        assert r["exchanged"] == {"spirit_stone": 100, "fateum": 150}
        assert self.account.get_balance("spirit_stone") == 400
        assert self.account.get_balance("fateum") == 150

    def test_exchange_insufficient(self):
        r = cross_sect_exchange(self.account, "spirit_stone", "destium", 1000)
        assert not r["ok"]
        assert "insufficient" in r["error"]

    def test_exchange_same_currency(self):
        r = cross_sect_exchange(self.account, "spirit_stone", "spirit_stone", 50)
        assert not r["ok"]
        assert "same" in r["error"].lower()

    def test_exchange_unknown_currency(self):
        r = cross_sect_exchange(self.account, "spirit_stone", "gold", 50)
        assert not r["ok"]
        assert "unknown" in r["error"].lower()


class TestSectShares:
    """RF2 / D10 - Sect ownership shares."""

    def setup_method(self):
        self.account = MultiCurrencyAccount(player_id="p4")
        earn_currency(self.account, "spirit_stone", 500)

    def test_buy_shares(self):
        r = buy_sect_share(self.account, "azure_cloud", 300)
        assert r["ok"]
        assert r["shares_purchased"] == 3
        assert r["total_shares"] == 3
        assert r["ownership_pct"] == 3
        assert self.account.get_balance("spirit_stone") == 200

    def test_buy_insufficient(self):
        r = buy_sect_share(self.account, "azure_cloud", 10000)
        assert not r["ok"]
        assert "insufficient" in r["error"]

    def test_buy_zero_cost(self):
        r = buy_sect_share(self.account, "azure_cloud", 0)
        assert not r["ok"]

    def test_share_pct_correct(self):
        # 100 spirit_stone = 1% ownership
        r = buy_sect_share(self.account, "azure_cloud", 100)
        assert r["ok"]
        assert r["ownership_pct"] == 1

    def test_get_share(self):
        buy_sect_share(self.account, "azure_cloud", 200)
        pct = get_sect_share(self.account, "azure_cloud")
        assert pct == 2
        assert get_sect_share(self.account, "nonexistent") == 0


class TestDaoInsight:
    """RF10 - Dao Insight tracking."""

    def setup_method(self):
        self.account = MultiCurrencyAccount(player_id="p5")
        earn_currency(self.account, "spirit_stone", 500)
        earn_currency(self.account, "souldust", 50)
        earn_currency(self.account, "destium", 20)
        earn_currency(self.account, "fateum", 10)

    def test_dao_summary(self):
        r = dao_insight_summary(self.account)
        assert r["spirit_stone"] == 500
        assert r["souldust"] == 50
        assert r["destium"] == 20
        assert r["fateum"] == 10
        assert r["total_currencies_held"] == 580
        assert r["active_shares"] == 0

    def test_dao_summary_after_shares(self):
        buy_sect_share(self.account, "azure_cloud", 100)
        r = dao_insight_summary(self.account)
        assert r["active_shares"] == 1
