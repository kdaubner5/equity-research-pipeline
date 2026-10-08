"""Stage-1 CLI/cron wrapper (Issue #9): the full funnel as one runnable script.

    python run_stage1.py

Wires together the layers that so far only ran inside tests
(tests/test_simfin_pilot_gates.py is a characterization snapshot, not a
runner): loads the Phase-1.0 ticker universe (config/universe.txt, see
docs/decisions.md "Issue #9 - CLI/Cron-Wrapper" - explicitly NOT the
market-cap/ADV pre-filter screener from masterplan.md, which does not exist
yet), fetches SimFin data, computes the three Stage-1 metrics, evaluates the
Stage-1 gates, and archives the whole funnel via the existing Store-Layer.

Per-ticker/per-batch failures are caught and turned into an eliminated
company with a runner-level invalid_reason (simfin_id_not_found,
simfin_statements_fetch_failed, simfin_statements_missing,
simfin_mapping_error, metric_input_error) - one bad ticker never aborts the
run for the rest (docs/decisions.md, Entscheidung 2). Exit code is 0 whenever the run
completed and wrote its results, regardless of how many companies survived;
non-zero only for a technical failure that prevented the run from producing
results at all (missing API key, companies/list unreachable, the store
layer failing to write) - Entscheidung 3.

Modular by design (masterplan.md §9, Prinzip 3): run() takes every path/key
as an optional argument so it can be driven by a cron job unmodified, or by
a test with temp paths and a mocked network (tests/test_run_stage1.py) -
main() just calls run() with every default.
"""

import sys
import urllib.error
from collections import Counter
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))  # run_stage1.py lives outside src/, same fixup as
# tests/test_simfin_pilot_gates.py uses to run as a plain script.

from config_layer.thresholds import (  # noqa: E402
    DEFAULT_THRESHOLDS_PATH,
    ThresholdsConfigError,
    compute_config_hash,
    load_thresholds,
)
from data_layer.simfin_client import (  # noqa: E402
    MAX_BATCH_SIZE,
    SimFinMappingError,
    fetch_companies_list,
    fetch_statements_batch,
    resolve_simfin_id,
)
from gates_layer.stage1_gates import (  # noqa: E402
    FAILED,
    NOT_EVALUABLE,
    PASSED,
    PASSED_WITH_FLAGS,
    UNSUPPORTED_BUSINESS_MODEL,
    GateResult,
)
from metrics_layer.result import MetricInputError  # noqa: E402
from pipeline import evaluate_company  # noqa: E402
from store_layer.db import DEFAULT_CSV_DIR, DEFAULT_DB_PATH, export_survivors_csv, save_run_results  # noqa: E402
from store_layer.rule_state import RuleStateError, capture_rule_state, require_same_bytes  # noqa: E402

DEFAULT_UNIVERSE_PATH = _REPO_ROOT / "config" / "universe.txt"
DEFAULT_ENV_PATH = _REPO_ROOT / ".env"

# Runner-level invalid_reasons (docs/decisions.md, Issue #9 Entscheidung 2) -
# distinct from gates_layer's per-metric reasons (insufficient_years, ...):
# these fire before any metric is even computed, on data availability alone.
SIMFIN_ID_NOT_FOUND = "simfin_id_not_found"
SIMFIN_STATEMENTS_FETCH_FAILED = "simfin_statements_fetch_failed"
SIMFIN_STATEMENTS_MISSING = "simfin_statements_missing"
SIMFIN_MAPPING_ERROR = "simfin_mapping_error"
# Unlike the four reasons above, this one fires *during* metric computation,
# not before it: a metrics_layer input-shape guard (duplicate fiscal_year,
# more than 3 periods) tripping instead of the usual gates_layer reasons
# (insufficient_years, ...). pipeline.evaluate_company() documents this as
# raisable alongside SimFinMappingError; code review of PR #32 found
# evaluate_ticker() wasn't catching it.
METRIC_INPUT_ERROR = "metric_input_error"

EXIT_OK = 0
EXIT_TECHNICAL_FAILURE = 1


class Stage1RunError(Exception):
    """A technical failure that aborts the whole run (Entscheidung 3) - never
    a single company's data problem, which is caught and eliminated instead."""


def load_universe(path=DEFAULT_UNIVERSE_PATH):
    """One ticker per line, blank lines skipped. See docs/decisions.md Entscheidung 1."""
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _load_dotenv_if_present(path=DEFAULT_ENV_PATH):
    """Populate os.environ from a simple KEY=VALUE .env file, without overriding
    a variable the environment already set. No-op if the file does not exist.

    Minimal, dependency-free parser (no python-dotenv in pyproject.toml) -
    matches masterplan.md's "radikal einfach" for a single-key cron need.
    """
    import os

    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def resolve_api_key():
    """SIMFIN_API_KEY from the environment, falling back to .env. Raises
    Stage1RunError if neither has it (Entscheidung 3: technical failure)."""
    import os

    _load_dotenv_if_present()
    api_key = os.environ.get("SIMFIN_API_KEY")
    if not api_key:
        raise Stage1RunError("SIMFIN_API_KEY not set (checked the environment and .env)")
    return api_key


def resolve_thresholds(path=DEFAULT_THRESHOLDS_PATH):
    """Load and validate config/thresholds.yaml, and its config_hash.

    Raises Stage1RunError (Entscheidung 3: technical failure, aborts the
    whole run before any per-ticker work) if the file is missing or fails
    validation - a bad thresholds file is a run-level infrastructure
    problem, exactly like a missing SIMFIN_API_KEY, never a per-ticker
    invalid_reason (docs/decisions.md, "Schwellen nach config/thresholds.yaml").
    """
    try:
        thresholds = load_thresholds(path)
    except ThresholdsConfigError as exc:
        raise Stage1RunError(f"config/thresholds.yaml invalid: {exc}") from exc
    return thresholds, compute_config_hash(path)


def _eliminated(reason):
    """A GateResult for a ticker whose data could not even be mapped - no
    metric was ever computed, so all four raw values stay None (same shape
    as a not_evaluable GateResult from evaluate_stage1_gates(), just built
    directly since there are no MetricResults to combine here)."""
    return GateResult(
        status=NOT_EVALUABLE,
        flags=(),
        invalid_reasons=(reason,),
        roic=None,
        net_debt_ebitda=None,
        cash_conversion=None,
        net_income_margin=None,
        data_quality_flags=(),
    )


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def evaluate_ticker(ticker, raw_company, *, thresholds):
    """One ticker through the whole Stage-1 chain, from already-fetched raw
    SimFin JSON to a GateResult, via the same pipeline.evaluate_company()
    that tests/test_simfin_pilot_gates.py uses - one shared wiring, not two
    independent copies. Both failure modes evaluate_company() documents
    (SimFinMappingError, MetricInputError) become an eliminated GateResult
    (Entscheidung 2) instead of propagating and aborting the whole run."""
    try:
        return evaluate_company(ticker, raw_company, thresholds=thresholds)
    except SimFinMappingError:
        return _eliminated(SIMFIN_MAPPING_ERROR)
    except MetricInputError:
        return _eliminated(METRIC_INPUT_ERROR)


def evaluate_universe(tickers, api_key, company_list, *, thresholds):
    """Resolve, fetch and evaluate every ticker. Returns {ticker: GateResult},
    covering every ticker in `tickers` exactly once - resolved-and-evaluated
    or eliminated (Entscheidung 2), never silently dropped."""
    gate_results = {}
    resolved_ids = {}
    for ticker in tickers:
        try:
            resolved_ids[ticker] = resolve_simfin_id(ticker, api_key, company_list=company_list)
        except SimFinMappingError:
            gate_results[ticker] = _eliminated(SIMFIN_ID_NOT_FOUND)

    resolved_tickers = list(resolved_ids)
    for batch in _chunk(resolved_tickers, MAX_BATCH_SIZE):
        ids = [resolved_ids[ticker] for ticker in batch]
        try:
            raw_records = fetch_statements_batch(ids, api_key)
        except (urllib.error.URLError, TimeoutError, ValueError):
            # URLError also covers HTTPError; ValueError also covers
            # json.JSONDecodeError - the genuine network/malformed-response
            # failure modes fetch_statements_batch() can raise, isolated to
            # this whole batch (not just one call type). Deliberately
            # narrower than a bare `except Exception` (code review of PR
            # #32): a programming bug elsewhere in that call should surface,
            # not be silently relabeled as an ordinary data problem.
            for ticker in batch:
                gate_results[ticker] = _eliminated(SIMFIN_STATEMENTS_FETCH_FAILED)
            continue

        # Keyed by the id we actually requested for each ticker, not by the
        # record's own self-reported "ticker" field: two records could
        # otherwise collide on the same ticker string (silently dropping
        # one), and a record with a null "ticker" (key present, value None)
        # would crash .upper() instead of being handled explicitly (code
        # review of PR #32). build_company_record()'s own ticker-mismatch
        # check (SimFinMappingError, now caught in evaluate_ticker()) is
        # still the last line of defense against a mispaired record.
        raw_by_id = {
            record["id"]: record
            for record in raw_records
            if isinstance(record, dict) and record.get("id") is not None
        }
        for ticker in batch:
            raw_company = raw_by_id.get(resolved_ids[ticker])
            if raw_company is None:
                gate_results[ticker] = _eliminated(SIMFIN_STATEMENTS_MISSING)
                continue
            gate_results[ticker] = evaluate_ticker(ticker, raw_company, thresholds=thresholds)

    return gate_results


def print_summary(gate_results, file=sys.stdout):
    counts = Counter(result.status for result in gate_results.values())
    print(
        f"{len(gate_results)} companies: "
        f"passed={counts[PASSED]}  passed_with_flags={counts[PASSED_WITH_FLAGS]}  "
        f"failed={counts[FAILED]}  not_evaluable={counts[NOT_EVALUABLE]}  "
        f"unsupported_business_model={counts[UNSUPPORTED_BUSINESS_MODEL]}",
        file=file,
    )
    # docs/decisions.md "Phase 2.0e": a run that applied no sector profile (the pilot runner)
    # says so instead of silently reading as "everything is in the standard profile".
    checked = [result for result in gate_results.values() if result.profile is not None]
    if not checked:
        print("profile check: not checked (no sector profiles applied)", file=file)
    else:
        borderline = sum(1 for result in checked if result.borderline_sector)
        print(
            f"profile check: {len(checked)} of {len(gate_results)} companies checked  borderline_sector={borderline}",
            file=file,
        )


def run(
    universe_path=DEFAULT_UNIVERSE_PATH,
    db_path=DEFAULT_DB_PATH,
    csv_dir=DEFAULT_CSV_DIR,
    api_key=None,
    run_timestamp=None,
    thresholds_path=DEFAULT_THRESHOLDS_PATH,
    out=sys.stdout,
):
    """One full Stage-1 run. Returns an exit code (Entscheidung 3) - never
    raises for a per-company problem, only for a technical failure that
    stops the run before results could be written."""
    try:
        if api_key is None:
            api_key = resolve_api_key()
        thresholds, config_hash = resolve_thresholds(thresholds_path)
        # Rule state once, at the start (docs/decisions.md "Regelstand und Kurs je Lauf", Punkt 6 und Frage 6): a
        # missing or unreadable rule file stops the run before anything is fetched or written.
        try:
            rule_state = capture_rule_state(rule_paths={"config/thresholds.yaml": thresholds_path})
            require_same_bytes(rule_state, "config/thresholds.yaml", config_hash)
        except RuleStateError as exc:
            raise Stage1RunError(str(exc)) from exc
        tickers = load_universe(universe_path)
        try:
            company_list = fetch_companies_list(api_key)
        except Exception as exc:
            raise Stage1RunError(f"companies/list unreachable: {exc}") from exc
        if not isinstance(company_list, list):
            # An HTTP-200 response that isn't the expected JSON list (e.g. an
            # error envelope like {"error": "..."}) isn't an exception, so
            # the try/except above wouldn't catch it - it would otherwise
            # only surface later, mid-ticker-loop, as a confusing crash
            # (code review of PR #32).
            raise Stage1RunError(
                f"companies/list returned an unexpected shape: {type(company_list).__name__}, expected a list"
            )

        gate_results = evaluate_universe(tickers, api_key, company_list, thresholds=thresholds)

        run_timestamp = run_timestamp or datetime.now().isoformat(timespec="seconds")
        # CSV first, DB write last: save_run_results() is the authoritative,
        # append-only archive (a plain INSERT, never overwritten - its own
        # docstring), so it must be the step that only ever happens once the
        # rest of the run is known-good. If export_survivors_csv() fails,
        # nothing has touched the DB yet. If save_run_results() then fails
        # after the CSV already landed, the CSV is removed again - either
        # way "nonzero exit code" keeps meaning "nothing was written"
        # (Entscheidung 3), instead of leaving a committed DB run with no
        # CSV, or a CSV with no corresponding archive entry (code review of
        # PR #32).
        csv_path = None
        try:
            csv_path = export_survivors_csv(run_timestamp, gate_results, output_dir=csv_dir)
            save_run_results(run_timestamp, gate_results, config_hash=config_hash, rule_state=rule_state, db_path=db_path)
        except Exception as exc:
            if csv_path is not None:
                csv_path.unlink(missing_ok=True)
            raise Stage1RunError(f"could not write run results: {exc}") from exc

        print(f"run_timestamp={run_timestamp}  survivors_csv={csv_path}", file=out)
        print_summary(gate_results, file=out)
        return EXIT_OK
    except Stage1RunError as exc:
        print(f"run_stage1: technical failure, run aborted: {exc}", file=sys.stderr)
        return EXIT_TECHNICAL_FAILURE


def main():
    return run()


if __name__ == "__main__":
    sys.exit(main())
