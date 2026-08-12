"""What the house actually cost, and what it would have cost otherwise.

Copyright (C) 2026 Tomas Smedberg.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed without any warranty; see the LICENSE file at
the repository root for the full terms.

Pure arithmetic, no Home Assistant. :mod:`metering` reads the meters and calls
in here; the sensors read the results back out. Keeping the money in one
side-effect-free module is what makes it testable against hand-worked figures,
which is the only way anyone will ever trust a savings number.

A saving is a difference against a counterfactual, so the counterfactual has to
be stated before the number means anything. This module settles three versions
of the same house against the same prices at the same instants:

* **W0, grid only** -- no PV, no battery. Every kWh the house consumes is
  bought at the retail price of the moment.
* **W1, solar only** -- PV offsets load as it arrives, the excess is exported.
  No battery.
* **W2, actual** -- what the meters recorded.

which decompose exactly, with nothing counted twice::

    solar savings   = C0 - C1
    battery savings = C1 - C2   (plus storage carry)
    total savings   = C0 - C2   (plus storage carry)

One property of that split is worth keeping in mind while reading the rest:
``C0`` depends only on house load and the buy price, and ``C2`` is read
straight off the grid meter, so **total savings does not depend on the PV
measurement at all**. Only the solar/battery split does, because only ``C1``
reads ``pv``. A DC-side PV meter or an optimistic inverter moves the split and
leaves the headline alone.

See ``docs/savings_plan.md`` for the design note and the alternatives rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any

from .models import Plan, PlanRow, Series

# Nothing in here is worth persisting or publishing below this: a settle of a
# hundredth of an ore is meter noise, and rounding at the edges keeps the
# stored ledger from accumulating float dust across a day of updates.
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class Prices:
    """What a kWh is worth in both directions at one instant.

    Both already composed by :mod:`tariff` -- taxes, grid fees, VAT and
    whatever else the user described are inside these numbers. Nothing here
    knows about a spot price.
    """

    buy: float
    sell: float


@dataclass(frozen=True, slots=True)
class IntervalEnergy:
    """Energy that moved during one micro-interval, in kWh, all non-negative.

    ``house_load`` is the one field that may be None. When a whole-house sensor
    is configured it is measured directly and used as-is; otherwise it is
    derived from the energy balance in :func:`house_load_kwh`, which is
    correct but inherits every other meter's error.
    """

    imported: float = 0.0
    exported: float = 0.0
    pv: float = 0.0
    battery_charge: float = 0.0
    battery_discharge: float = 0.0
    house_load: float | None = None

    def __bool__(self) -> bool:
        """Whether anything at all moved -- an all-zero interval is skipped."""
        return any(
            abs(value) > _EPSILON
            for value in (
                self.imported,
                self.exported,
                self.pv,
                self.battery_charge,
                self.battery_discharge,
                self.house_load or 0.0,
            )
        )


def house_load_kwh(energy: IntervalEnergy) -> float:
    """What the house consumed, measured if possible and derived if not.

    The derivation is the energy balance at the point of common coupling::

        pv + imported + discharge = load + exported + charge

    Clamped at zero. A negative result is not a house that generated
    consumption; it is meters that disagree -- most often a PV or battery
    sensor read on the DC side against an AC-side grid meter, or one source
    that updated a moment before the others. Letting it through would price
    negative load at the buy price and hand W0 a credit.
    """
    if energy.house_load is not None:
        return max(energy.house_load, 0.0)
    derived = (
        energy.pv
        + energy.imported
        + energy.battery_discharge
        - energy.exported
        - energy.battery_charge
    )
    return max(derived, 0.0)


def settle(net_kwh: float, prices: Prices) -> float:
    """Cost of holding a net grid position for one interval.

    ``net_kwh`` positive is import. Buying and selling are priced separately
    because they always are: the sell price is rarely the buy price, and in
    most markets is not close to it.

    The clipping here is exactly why :mod:`metering` settles on every state
    change rather than on a timer. Over a quarter of an hour a house that
    imported for five minutes and exported for ten nets out to one direction
    and the other side's money disappears; over a few seconds it does not.
    """
    return max(net_kwh, 0.0) * prices.buy - max(-net_kwh, 0.0) * prices.sell


@dataclass(frozen=True, slots=True)
class IntervalCosts:
    """One micro-interval settled in all three worlds."""

    actual: float
    """W2. Measured import and export, priced separately -- deliberately not
    ``settle(imported - exported)``. Both directions inside one interval is a
    real thing a meter can report, and netting them away would throw
    information the meter went to the trouble of keeping."""
    solar_only: float
    """W1: the same house and the same PV, with the battery never used."""
    grid_only: float
    """W0: the same house with neither, buying every kWh it consumes."""
    load: float
    """House load for the interval, kWh -- resolved once here so the ledger
    does not have to re-derive it."""


def cost_worlds(energy: IntervalEnergy, prices: Prices) -> IntervalCosts:
    """Settle one micro-interval in W0, W1 and W2."""
    load = house_load_kwh(energy)
    return IntervalCosts(
        actual=energy.imported * prices.buy - energy.exported * prices.sell,
        solar_only=settle(load - energy.pv, prices),
        grid_only=load * prices.buy,
        load=load,
    )


@dataclass(slots=True)
class Ledger:
    """Running totals for one accounting day.

    Persisted verbatim through :mod:`homeassistant.helpers.storage`, so every
    field is a plain float or string and :meth:`as_dict` / :meth:`from_dict`
    are the whole serialisation story. Restoring an unknown field is tolerated
    (a downgrade) and a missing one defaults, so a ledger written by a newer
    version never takes the integration down mid-day.
    """

    day: str = ""
    """Local ISO date this ledger is accumulating. The rollover trigger."""

    actual_cost: float = 0.0
    solar_only_cost: float = 0.0
    grid_only_cost: float = 0.0

    imported_kwh: float = 0.0
    exported_kwh: float = 0.0
    pv_kwh: float = 0.0
    house_load_kwh: float = 0.0
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0

    import_spend: float = 0.0
    """Money on imports alone, before export income. Published because "what
    did I spend" and "what did the grid net out to" are different questions and
    users ask both."""
    export_income: float = 0.0

    charge_price_weighted: float = 0.0
    """Sum of ``charge_kwh * buy`` -- the numerator of the charge-weighted
    average price, which is both a published figure and the price the storage
    carry is valued at."""
    discharge_price_weighted: float = 0.0
    charge_priced_kwh: float = 0.0
    """Only the charging that happened while a price was known. Dividing by
    ``battery_charge_kwh`` instead would drag the average toward zero for
    every unpriced kWh."""
    discharge_priced_kwh: float = 0.0

    unpriced_kwh: float = 0.0
    """Grid energy seen while no price was known -- before the first run of a
    restart, or a hole in the price series. Counted but not costed, and
    published, so an incomplete day is visibly incomplete instead of quietly
    cheap."""

    stored_start_kwh: float | None = None
    """Battery contents when the day opened, from SOC and capacity. Reported,
    and used to reconcile the cost account below against the battery's own
    idea of how full it is."""
    stored_now_kwh: float | None = None

    # -- the battery's cost account -------------------------------------------
    #
    # A running weighted-average inventory, exactly as a warehouse would value
    # stock: charging adds energy at what it cost, discharging removes it at
    # the account's current unit price, and the day's carry is the change in
    # the account's value.
    #
    # The obvious cheaper scheme -- value the day's SOC delta at the day's own
    # charge price -- is wrong, and wrong asymmetrically. On a day that only
    # charges, that price exists; on a day that only discharges (the second
    # half of every single arbitrage cycle) there is nothing to take it from,
    # the debit silently becomes zero, and the day keeps the entire displaced
    # retail value as profit. A cycle split across midnight then reports the
    # purchase for free and the sale at full value. The account below is what
    # makes the pair of days add up to the spread that was actually captured.
    battery_book_kwh: float | None = None
    """Energy the cost account believes is in the battery. None until the first
    priced interval, which is when the account is opened."""
    battery_book_value: float = 0.0
    """Money tied up in that energy."""
    battery_book_value_start: float = 0.0
    """The account's value when the day opened. The carry is the difference."""

    balance_residual_kwh: float = 0.0
    """Signed disagreement between the measured house load and the one the
    energy balance implies, accumulated over the day. Zero when there is no
    measured house-load sensor to disagree with. A free consistency check on
    the user's own meters, and the first thing to look at when a number seems
    wrong."""

    def record(self, energy: IntervalEnergy, prices: Prices | None) -> None:
        """Fold one micro-interval into the day.

        ``prices`` of None means the price series could not answer for this
        instant. The energy still counts -- pretending it did not flow would
        make the kWh totals wrong as well as the money -- but no cost is
        accrued and the grid side of it lands in ``unpriced_kwh``.
        """
        if not energy:
            return

        costs = cost_worlds(energy, prices) if prices else None
        load = costs.load if costs else house_load_kwh(energy)

        self.imported_kwh += energy.imported
        self.exported_kwh += energy.exported
        self.pv_kwh += energy.pv
        self.battery_charge_kwh += energy.battery_charge
        self.battery_discharge_kwh += energy.battery_discharge
        self.house_load_kwh += load

        if energy.house_load is not None:
            implied = (
                energy.pv
                + energy.imported
                + energy.battery_discharge
                - energy.exported
                - energy.battery_charge
            )
            self.balance_residual_kwh += energy.house_load - implied

        if prices is None or costs is None:
            self.unpriced_kwh += energy.imported + energy.exported
            return

        self.actual_cost += costs.actual
        self.solar_only_cost += costs.solar_only
        self.grid_only_cost += costs.grid_only
        self.import_spend += energy.imported * prices.buy
        self.export_income += energy.exported * prices.sell

        self.charge_price_weighted += energy.battery_charge * prices.buy
        self.charge_priced_kwh += energy.battery_charge
        self.discharge_price_weighted += energy.battery_discharge * prices.buy
        self.discharge_priced_kwh += energy.battery_discharge

        self._post_battery(energy, prices)

    def _post_battery(self, energy: IntervalEnergy, prices: Prices) -> None:
        """Move this interval's battery flows through the cost account."""
        if self.battery_book_kwh is None:
            # Opening the account. Whatever is already in the battery is
            # marked to the price in force now -- there is no record of what it
            # actually cost, and the alternative (valuing it at nothing) would
            # hand the first evening's discharge a free windfall. Wrong once,
            # by less than one charge's worth, and self-correcting from the
            # first real cycle onward.
            self.battery_book_kwh = self.stored_now_kwh or 0.0
            self.battery_book_value = self.battery_book_kwh * prices.buy
            self.battery_book_value_start = self.battery_book_value

        if energy.battery_charge > _EPSILON:
            self.battery_book_kwh += energy.battery_charge
            self.battery_book_value += energy.battery_charge * prices.buy

        if energy.battery_discharge > _EPSILON:
            # Weighted average cost: what leaves is valued at the account's
            # blended unit price, not at what it is displacing. The difference
            # between the two *is* the saving, and it belongs in
            # ``battery_savings``, not hidden inside the carry.
            unit = (
                self.battery_book_value / self.battery_book_kwh
                if self.battery_book_kwh > _EPSILON
                else prices.buy
            )
            drawn = min(energy.battery_discharge, self.battery_book_kwh)
            self.battery_book_kwh -= drawn
            self.battery_book_value -= drawn * unit
            if self.battery_book_kwh <= _EPSILON:
                # Drained past what the account knew about -- round-trip losses
                # and SOC drift both push this way. Zero the value too rather
                # than leaving money attached to no energy.
                self.battery_book_kwh = 0.0
                self.battery_book_value = 0.0

    def reconcile_storage(self, stored_kwh: float | None) -> None:
        """Pull the cost account back onto what the battery actually holds.

        Called at the day boundary. The account tracks the charge and discharge
        meters; the SOC sensor is an independent measurement of the same thing,
        and over weeks they drift -- efficiency the meters do not see, standby
        losses, a recalibrating BMS. Left alone the account would slowly stop
        describing the battery.

        The unit price is kept and the quantity corrected, so a reconciliation
        revalues the stock without inventing or destroying a price.
        """
        if stored_kwh is None or self.battery_book_kwh is None:
            return
        if self.battery_book_kwh > _EPSILON:
            unit = self.battery_book_value / self.battery_book_kwh
            self.battery_book_value = stored_kwh * unit
        self.battery_book_kwh = stored_kwh

    # -- derived figures ------------------------------------------------------

    @property
    def average_import_price(self) -> float | None:
        if self.imported_kwh <= _EPSILON:
            return None
        return self.import_spend / self.imported_kwh

    @property
    def average_export_price(self) -> float | None:
        if self.exported_kwh <= _EPSILON:
            return None
        return self.export_income / self.exported_kwh

    @property
    def average_charge_price(self) -> float | None:
        """What the battery paid, per kWh in, counting only priced charging."""
        if self.charge_priced_kwh <= _EPSILON:
            return None
        return self.charge_price_weighted / self.charge_priced_kwh

    @property
    def average_discharge_price(self) -> float | None:
        """What the battery displaced, per kWh out, at the buy price of the
        moment. Against :attr:`average_charge_price` this is the spread the
        battery worked -- before losses, which is what makes
        :attr:`battery_savings` the number to quote and this pair the
        explanation."""
        if self.discharge_priced_kwh <= _EPSILON:
            return None
        return self.discharge_price_weighted / self.discharge_priced_kwh

    @property
    def carry_price(self) -> float | None:
        """What one stored kWh is currently carried at, per the cost account."""
        if not self.battery_book_kwh or self.battery_book_kwh <= _EPSILON:
            return None
        return self.battery_book_value / self.battery_book_kwh

    @property
    def storage_carry(self) -> float:
        """Change in the value of what is sitting in the battery.

        A day that ends fuller than it started bought energy it has not used
        yet. Charged to that day alone it looks ruinous and the next day looks
        free, which is how a daily savings figure ends up sawtoothing between
        two numbers that are each wrong by the same amount.

        Because this is the change in a *running account* rather than a
        re-valuation of a delta, it telescopes exactly: the credit one day
        takes for storing energy is the identical debit the day that spends it
        pays, so any run of days sums to the same total with the term or
        without it. That is the property the whole daily figure rests on, and
        it is asserted directly in ``tests/test_savings.py``.
        """
        return self.battery_book_value - self.battery_book_value_start

    @property
    def solar_savings(self) -> float:
        """C0 - C1. Self-consumed PV at the buy price, plus export income."""
        return self.grid_only_cost - self.solar_only_cost

    @property
    def battery_savings(self) -> float:
        """C1 - C2 plus carry. Arbitrage, plus the self-consumption the
        battery bought by moving PV into the evening, minus the export revenue
        it gave up and minus round-trip losses -- which need no separate term
        because they are already the gap between charge and discharge."""
        return self.solar_only_cost - self.actual_cost + self.storage_carry

    @property
    def total_savings(self) -> float:
        return self.grid_only_cost - self.actual_cost + self.storage_carry

    @property
    def savings_excl_carry(self) -> float:
        return self.grid_only_cost - self.actual_cost

    @property
    def self_sufficiency(self) -> float | None:
        """Share of house load not bought from the grid, as a percentage."""
        if self.house_load_kwh <= _EPSILON:
            return None
        from_grid = min(self.imported_kwh, self.house_load_kwh)
        return 100.0 * (1.0 - from_grid / self.house_load_kwh)

    # -- persistence ----------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Ledger:
        """Restore a stored ledger, ignoring anything it does not recognise.

        Tolerant on purpose. This runs during setup, and a ledger written by a
        newer version -- a restored backup, a downgrade -- must not be able to
        take the integration down over a bookkeeping field.
        """
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


# -- forecast -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Forecast:
    """The same three worlds, evaluated over a plan instead of the meters."""

    start: datetime
    end: datetime
    hours: float
    """How much of the requested window the plan actually covered. Published
    rather than extrapolated: a 12-hour horizon cannot answer a 24-hour
    question, and saying so is more useful than doubling a number."""
    rows: int

    actual_cost: float
    solar_only_cost: float
    grid_only_cost: float
    storage_carry: float

    import_kwh: float
    export_kwh: float
    pv_kwh: float
    load_kwh: float
    hourly: list[tuple[datetime, float]] = field(default_factory=list)
    """Planned net cost per clock hour, for a card to draw. The shape a user
    needs to see *why* a day is expensive, which a single total cannot show."""

    @property
    def solar_savings(self) -> float:
        return self.grid_only_cost - self.solar_only_cost

    @property
    def battery_savings(self) -> float:
        return self.solar_only_cost - self.actual_cost + self.storage_carry

    @property
    def total_savings(self) -> float:
        return self.grid_only_cost - self.actual_cost + self.storage_carry

    @property
    def complete(self) -> bool:
        """Whether the plan covered the whole requested window."""
        return self.hours >= (self.end - self.start).total_seconds() / 3600 - 1e-6


def forecast_costs(
    plan: Plan | None,
    *,
    start: datetime,
    window: timedelta,
    buy: Series,
    sell: Series,
    stored_now_kwh: float | None = None,
    capacity_kwh: float | None = None,
) -> Forecast | None:
    """Price the plan ahead in all three worlds.

    Returns None when there is no plan, or none of it lies ahead -- an
    unavailable sensor being the honest answer there, rather than a zero that
    reads as "this day is free".

    Prices come from the plan's own ``unit_load_cost``/``unit_prod_price``
    where present, because those are what EMHASS actually optimised against and
    the plan only makes sense read against them. The Companion's own series is
    the fallback for rows that carry neither, which happens on older schemas.
    """
    if plan is None or not plan.rows:
        return None

    end = start + window
    step = plan.step or timedelta(minutes=30)

    actual = solar_only = grid_only = 0.0
    import_kwh = export_kwh = pv_kwh = load_kwh = import_spend = 0.0
    charge_weighted = charge_kwh = 0.0
    # Time-weighted, not import-weighted: the fallback price for the carry term
    # has to exist even on a window the plan never imports in, which is exactly
    # the shape of a sunny day that ends with a full battery.
    buy_hours = buy_weighted = 0.0
    covered = timedelta()
    rows = 0
    hourly: dict[datetime, float] = {}
    soc_last: float | None = None

    for row in plan.rows:
        row_end = row.timestamp + step
        # Hold-last on the leading edge, matching Plan.row_at: the row "now"
        # sits inside is part of the window even though it began before it, and
        # only the part still ahead is counted.
        overlap_start = max(row.timestamp, start)
        overlap_end = min(row_end, end)
        if overlap_end <= overlap_start:
            continue
        hours = (overlap_end - overlap_start).total_seconds() / 3600
        if hours <= 0:
            continue

        prices = _row_prices(row, buy, sell)
        if prices is None:
            continue

        grid_kw = (row.p_grid or 0.0) / 1000.0
        pv_kw = (row.p_pv or 0.0) / 1000.0
        # P_Load excludes the deferrables in EMHASS's schema, so they are added
        # back: W0 has to buy that energy too, and a baseline that ignored the
        # car charger would understate what the house avoided.
        load_kw = ((row.p_load or 0.0) + sum(row.deferrables)) / 1000.0
        batt_kw = (row.p_batt or 0.0) / 1000.0

        energy = IntervalEnergy(
            imported=max(grid_kw, 0.0) * hours,
            exported=max(-grid_kw, 0.0) * hours,
            pv=pv_kw * hours,
            house_load=load_kw * hours,
        )
        costs = cost_worlds(energy, prices)

        actual += costs.actual
        solar_only += costs.solar_only
        grid_only += costs.grid_only
        import_kwh += energy.imported
        import_spend += energy.imported * prices.buy
        export_kwh += energy.exported
        pv_kwh += energy.pv
        load_kwh += costs.load
        # p_batt is positive while discharging, so charging is the negative
        # side. Weighted at the buy price for the same reason the ledger does:
        # it is what the stored kWh will have cost.
        charge = max(-batt_kw, 0.0) * hours
        charge_weighted += charge * prices.buy
        charge_kwh += charge
        buy_weighted += prices.buy * hours
        buy_hours += hours

        hour = overlap_start.replace(minute=0, second=0, microsecond=0)
        hourly[hour] = hourly.get(hour, 0.0) + costs.actual

        covered += overlap_end - overlap_start
        rows += 1
        if row.soc is not None:
            soc_last = row.soc

    if not rows:
        return None

    carry = _forecast_carry(
        soc_last=soc_last,
        stored_now_kwh=stored_now_kwh,
        capacity_kwh=capacity_kwh,
        charge_weighted=charge_weighted,
        charge_kwh=charge_kwh,
        buy_weighted=buy_weighted,
        buy_hours=buy_hours,
    )

    return Forecast(
        start=start,
        end=end,
        hours=covered.total_seconds() / 3600,
        rows=rows,
        actual_cost=actual,
        solar_only_cost=solar_only,
        grid_only_cost=grid_only,
        storage_carry=carry,
        import_kwh=import_kwh,
        export_kwh=export_kwh,
        pv_kwh=pv_kwh,
        load_kwh=load_kwh,
        hourly=sorted(hourly.items()),
    )


def _row_prices(row: PlanRow, buy: Series, sell: Series) -> Prices | None:
    """The buy/sell pair in force for one plan row.

    The plan's own columns first: they are what EMHASS solved against, and
    reading the plan against any other prices produces a cost the plan was
    never trying to achieve. Our composed series is the fallback for schemas
    that omit the columns, and a row neither can answer is skipped rather than
    priced at zero.
    """
    buy_price = row.unit_load_cost
    if buy_price is None:
        buy_price = buy.value_at(row.timestamp) if buy else None
    sell_price = row.unit_prod_price
    if sell_price is None:
        sell_price = sell.value_at(row.timestamp) if sell else None
    if buy_price is None:
        return None
    return Prices(buy=float(buy_price), sell=float(sell_price or 0.0))


def _forecast_carry(
    *,
    soc_last: float | None,
    stored_now_kwh: float | None,
    capacity_kwh: float | None,
    charge_weighted: float,
    charge_kwh: float,
    buy_weighted: float,
    buy_hours: float,
) -> float:
    """Value of the energy the plan expects to leave in the battery.

    The same correction the realised ledger applies, for the same reason: a
    horizon that ends with a full battery has bought energy it has not spent,
    and charging the window for it makes every overnight-charging plan look
    ruinous. Measured SOC is the anchor, not the plan's first row -- the
    question is what happens *from here*.

    Silently zero without a capacity, an SOC reading and a planned terminal
    SOC. Each of those is a thing the user may simply not have configured, and
    a savings figure that quietly drops one term is better than one that
    invents it.
    """
    if soc_last is None or stored_now_kwh is None or not capacity_kwh:
        return 0.0
    stored_end = soc_last * capacity_kwh
    if charge_kwh > _EPSILON:
        # What the plan expects to pay for what it puts in.
        price = charge_weighted / charge_kwh
    elif buy_hours > _EPSILON:
        # A window that only discharges still has to debit what it spends, or
        # the evening half of an arbitrage cycle books the whole displaced
        # retail value as profit. The window's own average buy price is the
        # available stand-in; unlike the realised ledger there is no cost
        # account to consult, because a forecast has no history.
        price = buy_weighted / buy_hours
    else:
        return 0.0
    return (stored_end - stored_now_kwh) * price
