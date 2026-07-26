"""Unit tests for `store.classify_sync` — the explicit, directly-testable
status classifier that replaced the implicit `pages_seen`/`succeeded_any`
conditional chain responsible for the 2026-07-26 sync-health incident (see
`test_sync_health.py` for the full incident-shaped regression scenarios and
`store.py`'s module docstring above `SOFT_FAIL_PARTIAL_RATIO` for the root
cause). These tests require no database — `classify_sync` is a pure function
of a `SourceOutcome` plus two booleans.

The table below exercises every branch of `classify_sync`'s documented rule
order, not just the three sync-health incident scenarios:

  1. cancelled by user                              -> failed
  2. nothing indexed or confirmed this run           -> failed
  3. purge_guard_refused                             -> partial
  4. crawl_aborted_early                             -> partial
  5. pages_failed > 0                                -> partial
  6. soft-fail ratio > SOFT_FAIL_PARTIAL_RATIO        -> partial
  7. otherwise                                       -> ok

Rule order also matters: earlier rules must win even when a later rule's
condition also holds (e.g. cancellation must report "failed" even if pages
were fetched, or even if the purge guard also refused).
"""

from __future__ import annotations

import pytest
from app.store import SOFT_FAIL_PARTIAL_RATIO, SourceOutcome, classify_sync


def _outcome(**overrides) -> SourceOutcome:
    defaults = dict(
        name="t",
        pages_fetched=0,
        pages_skipped=0,
        pages_not_modified=0,
        pages_failed=0,
        pages_soft_failed=0,
        pages_removed=0,
        chunks_indexed=0,
        error=None,
    )
    defaults.update(overrides)
    return SourceOutcome(**defaults)


# --- Rule 1: cancelled by user always wins, regardless of every other signal.
def test_cancelled_by_user_is_failed_even_with_successes():
    outcome = _outcome(pages_fetched=10, error="Aborted by user")
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "failed"


def test_cancelled_by_user_outranks_purge_guard_and_abort():
    outcome = _outcome(pages_fetched=10, error="Aborted by user")
    assert classify_sync(outcome, crawl_aborted_early=True, purge_guard_refused=True) == "failed"


# --- Rule 2: nothing indexed or confirmed this run.
def test_zero_fetched_skipped_not_modified_is_failed_with_soft_failures_only():
    # The core T0 regression: an entirely soft-failed crawl (0 hard failures)
    # must not fall through to "ok" just because pages_failed == 0.
    outcome = _outcome(pages_soft_failed=40)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "failed"


def test_zero_fetched_skipped_not_modified_is_failed_with_zero_pages_seen():
    outcome = _outcome()
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "failed"


def test_zero_fetched_skipped_not_modified_is_failed_even_with_hard_failures():
    outcome = _outcome(pages_failed=5)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "failed"


def test_nonzero_pages_not_modified_alone_avoids_the_empty_crawl_failed_rule():
    # Preserves 1c8d8ba: an all-304 sync legitimately reports "ok".
    outcome = _outcome(pages_not_modified=1)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "ok"


def test_nonzero_pages_skipped_alone_avoids_the_empty_crawl_failed_rule():
    outcome = _outcome(pages_skipped=1)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "ok"


def test_nonzero_pages_fetched_alone_avoids_the_empty_crawl_failed_rule():
    outcome = _outcome(pages_fetched=1)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "ok"


# --- Rule 3: purge_guard_refused.
def test_purge_guard_refused_is_partial_despite_zero_hard_failures():
    outcome = _outcome(pages_fetched=1)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=True) == "partial"


def test_purge_guard_refused_outranks_soft_fail_ratio_being_fine():
    outcome = _outcome(pages_fetched=100, pages_soft_failed=0)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=True) == "partial"


# --- Rule 4: crawl_aborted_early.
def test_crawl_aborted_early_is_partial_even_when_every_page_seen_succeeded():
    outcome = _outcome(pages_fetched=5)
    assert classify_sync(outcome, crawl_aborted_early=True, purge_guard_refused=False) == "partial"


# --- Rule 4b: crawl_truncated (T5 -- sitemap max_pages truncation).
def test_crawl_truncated_is_partial_even_when_every_page_seen_succeeded():
    # gemini-api/google-search-console-api shaped: every page the (capped)
    # crawl reached was fetched successfully, but the enumeration itself was
    # provably incomplete -- this must still demote to "partial".
    outcome = _outcome(pages_fetched=500)
    assert (
        classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False, crawl_truncated=True)
        == "partial"
    )


def test_crawl_truncated_defaults_to_false_and_does_not_affect_otherwise_clean_sync():
    outcome = _outcome(pages_fetched=5)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "ok"


def test_crawl_truncated_outranked_by_cancellation():
    outcome = _outcome(pages_fetched=10, error="Aborted by user")
    assert (
        classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False, crawl_truncated=True)
        == "failed"
    )


# --- Rule 5: hard failures.
def test_any_hard_failure_is_partial_when_something_else_succeeded():
    outcome = _outcome(pages_fetched=1, pages_failed=1)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "partial"


def test_hard_failure_ratio_does_not_matter_only_presence_does():
    # A single hard failure out of hundreds of successes is still "partial" --
    # unlike soft failures, there is no ratio floor for pages_failed.
    outcome = _outcome(pages_fetched=999, pages_failed=1)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "partial"


# --- Rule 6: soft-failure ratio.
def test_soft_fail_ratio_at_exactly_the_floor_is_still_ok():
    # ratio == SOFT_FAIL_PARTIAL_RATIO is NOT "> the floor" -> still ok.
    fetched = 8
    soft_failed = 2
    assert soft_failed / (fetched + soft_failed) == SOFT_FAIL_PARTIAL_RATIO
    outcome = _outcome(pages_fetched=fetched, pages_soft_failed=soft_failed)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "ok"


def test_soft_fail_ratio_just_over_the_floor_is_partial():
    fetched = 7
    soft_failed = 2
    assert soft_failed / (fetched + soft_failed) > SOFT_FAIL_PARTIAL_RATIO
    outcome = _outcome(pages_fetched=fetched, pages_soft_failed=soft_failed)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "partial"


def test_traefik_incident_shaped_ratio_is_partial():
    # 280 pages attempted, 163 succeed, 117 soft-fail (42% content loss).
    outcome = _outcome(pages_fetched=163, pages_soft_failed=117)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "partial"


def test_small_incidental_soft_failure_count_stays_ok():
    outcome = _outcome(pages_fetched=200, pages_soft_failed=1)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "ok"


# --- Rule 7: otherwise ok.
def test_clean_sync_with_a_mix_of_fetched_skipped_not_modified_is_ok():
    outcome = _outcome(pages_fetched=3, pages_skipped=4, pages_not_modified=2)
    assert classify_sync(outcome, crawl_aborted_early=False, purge_guard_refused=False) == "ok"


@pytest.mark.parametrize(
    ("kwargs", "crawl_aborted_early", "purge_guard_refused", "expected"),
    [
        (dict(error="Aborted by user"), False, False, "failed"),
        (dict(), False, False, "failed"),
        (dict(pages_fetched=1), False, False, "ok"),
        (dict(pages_fetched=1), False, True, "partial"),
        (dict(pages_fetched=1), True, False, "partial"),
        (dict(pages_fetched=1, pages_failed=1), False, False, "partial"),
        (dict(pages_fetched=7, pages_soft_failed=117), False, False, "partial"),
    ],
)
def test_classify_sync_truth_table(kwargs, crawl_aborted_early, purge_guard_refused, expected):
    outcome = _outcome(**kwargs)
    assert (
        classify_sync(
            outcome,
            crawl_aborted_early=crawl_aborted_early,
            purge_guard_refused=purge_guard_refused,
        )
        == expected
    )
