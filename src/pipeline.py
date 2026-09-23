"""Stage-1 per-company pipeline: raw SimFin JSON -> GateResult.

Shared by run_stage1.py (Issue #9's runner) and
tests/test_simfin_pilot_gates.py's evaluate_pilot() - both need the exact
same wiring (build_company_record -> the three metrics -> evaluate_stage1_gates
-> collect_data_quality_flags), and keeping it in one place means a
metrics_layer signature change or a new metric is applied to both instead of
the two independent copies silently drifting apart (code review of PR #30,
docs/decisions.md: "Suggest: when Issue #9 lands, extract the per-company
pipeline (raw JSON -> GateResult) into a small src/-side function that both
the runner and this test's evaluate_pilot() call" - this module is that
extraction).
"""

from data_layer.simfin_client import build_company_record
from gates_layer.stage1_gates import collect_data_quality_flags, evaluate_stage1_gates
from metrics_layer.cash_conversion import cash_conversion_3yr_average, net_income_margin_3yr_average
from metrics_layer.net_debt_ebitda import net_debt_ebitda_latest
from metrics_layer.roic import roic_3yr_average


def evaluate_company(ticker, raw_company):
    """One company's raw SimFin JSON through the whole Stage-1 chain.

    Raises SimFinMappingError (from build_company_record) or MetricInputError
    (from a metrics_layer function, e.g. more than 3 periods or a duplicate
    fiscal_year - not expected from map_simfin_to_periods's own output today,
    but not this function's job to guard against) if the data cannot be
    turned into a GateResult at all. Callers decide how to handle that:
    run_stage1.py catches it per ticker (docs/decisions.md, Issue #9
    Entscheidung 2); the pilot snapshot test lets it propagate, since a
    mapping failure there is a test-fixture bug, not a runtime data problem.
    """
    periods = build_company_record(ticker, raw_company)["periods"]
    return evaluate_stage1_gates(
        roic=roic_3yr_average(periods),
        net_debt_ebitda=net_debt_ebitda_latest(periods),
        cash_conversion=cash_conversion_3yr_average(periods),
        net_income_margin=net_income_margin_3yr_average(periods),
        data_quality_flags=collect_data_quality_flags(periods),
    )
