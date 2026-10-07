"""Tests for the pure filter functions.

filters.py holds every decision made before an API call is spent: location,
age, federal eligibility, and the two-track keyword score. None of it touches
the network, which is the reason it was pulled out of job_alert.py — that
module scores jobs at import time, so nothing inside it could be imported by
a test without running the whole monitor.

Several of these are regressions for bugs that reached production and were
found by reading rejection logs.

Note on the scoring assertions: they compare the two track totals to each
other rather than to absolute numbers. Both tracks receive the same BROAD and
federal-bonus points, so their difference isolates the CORE_AI/CORE_OPS
contribution. That keeps these tests meaningful when the keyword lists are
retuned, which they are, often.
"""
from datetime import datetime, timedelta, timezone

import pytest

from filters import (MAX_AGE_DAYS, MIN_SCORE, fed_bonus, fed_reject, loc_ok,
                     score, score_tracks, too_old, track_of)


class Job:
    """Minimal stand-in for a scraped posting.

    Each scraper returns its own class; all of them expose these fields, and
    several leave some of them None. The defaults here are deliberately the
    empty/missing case so a test has to opt in to every field it cares about.
    """

    def __init__(self, title="", description="", location="",
                 is_remote=None, raw=None, posted_at=None):
        self.title = title
        self.description = description
        self.location = location
        self.is_remote = is_remote
        self.raw = raw
        self.posted_at = posted_at


def federal(code):
    """A USAJOBS raw payload carrying one occupational series code."""
    return {"JobCategory": [{"Code": code}]}


def days_ago(n, naive=False):
    t = datetime.now(timezone.utc) - timedelta(days=n)
    return t.replace(tzinfo=None) if naive else t


# --------------------------------------------------------------------------
# loc_ok
# --------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["Newark, NJ", "Manhattan, NY", "Jersey City, NJ"])
def test_commutable_metro_locations_pass(where):
    assert loc_ok(Job(location=where)) is True


def test_remote_in_the_location_string_passes():
    assert loc_ok(Job(location="Remote - US")) is True


def test_remote_flag_passes_when_the_location_is_unrecognised():
    """Boards that put nothing useful in the location field still set a flag."""
    assert loc_ok(Job(location="", is_remote=True)) is True


@pytest.mark.parametrize("where", ["Dublin, Ireland", "Tokyo, Japan"])
def test_foreign_locations_are_rejected(where):
    assert loc_ok(Job(location=where)) is False


def test_known_us_city_outside_the_metro_is_rejected():
    assert loc_ok(Job(location="Austin, TX")) is False


def test_unknown_onsite_location_is_rejected_by_default():
    """REGRESSION. NYNJ used to contain a bare ", ny", so the whole state
    passed — a Concrete Field Engineer in Baldwinsville, four hours away,
    cleared the filter. The list is now an explicit metro allowlist, and
    anything it doesn't name falls through to the remote flag.

    Fail-closed is the right default here: an unrecognised town costs one
    missed posting, while an unrecognised state costs an alert for a job that
    can't be taken."""
    assert loc_ok(Job(location="Baldwinsville, NY")) is False


def test_missing_location_does_not_raise():
    assert loc_ok(Job(location=None)) is False


# --------------------------------------------------------------------------
# score_tracks / track_of / score
# --------------------------------------------------------------------------

def test_core_ai_term_in_the_title_scores_the_ai_track_only():
    ai, ops = score_tracks(Job(title="RLHF Specialist"))
    assert ai - ops == 10


def test_core_ops_term_in_the_title_scores_the_ops_track_only():
    ai, ops = score_tracks(Job(title="AI Operations Lead"))
    assert ops - ai == 10


def test_a_description_match_is_worth_less_than_a_title_match():
    """3 in the body against 10 in the title. A posting that mentions RLHF in
    passing is not an RLHF role."""
    ai, ops = score_tracks(Job(description="Supports an rlhf pipeline."))
    assert ai - ops == 3


def test_broad_terms_count_toward_both_tracks_equally():
    """A 'program manager' title supports an ops match and an AI-ops match
    equally. Splitting the shared points would under-score exactly the
    overlap roles the resume is written for."""
    ai, ops = score_tracks(Job(description="program manager, quality"))
    assert ai == ops and ai > 0


def test_federal_series_bonus_counts_toward_both_tracks():
    ai, ops = score_tracks(Job(raw=federal("0343")))
    assert ai == ops == 12


def test_disqualifying_title_zeroes_both_tracks():
    """The disqualifier wins outright — it is not outweighed by keywords in
    the body. A sales role with 'rlhf' in the blurb is still a sales role."""
    assert score_tracks(Job(title="Sales Engineer",
                            description="rlhf annotation labeling")) == (0, 0)


def test_score_reports_the_stronger_of_the_two_tracks():
    j = Job(title="RLHF Specialist")
    assert score(j) == max(score_tracks(j))


def test_both_is_returned_when_each_track_clears_the_threshold():
    assert track_of(MIN_SCORE, MIN_SCORE) == "BOTH"


def test_single_track_above_threshold_is_labelled():
    assert track_of(MIN_SCORE, 0) == "AI"
    assert track_of(0, MIN_SCORE) == "OPS"


def test_nothing_above_threshold_returns_none():
    """None is the signal to drop the posting without spending an LLM call."""
    assert track_of(MIN_SCORE - 1, MIN_SCORE - 1) is None


# --------------------------------------------------------------------------
# federal eligibility
# --------------------------------------------------------------------------

def test_merit_promotion_language_in_the_description_is_rejected():
    """The API's own HiringPath=public filter still returns these, which is
    why the phrase check exists at all."""
    assert fed_reject(Job(description="Open to status candidates only.")) is True


def test_merit_language_in_the_qualification_summary_is_also_caught():
    """Layer three. Some postings carry the restriction only in the
    qualification text, not the description."""
    j = Job(raw={"QualificationSummary": "Open to current federal employees."})
    assert fed_reject(j) is True


def test_a_public_federal_posting_passes():
    j = Job(description="Open to all U.S. citizens.",
            raw={"QualificationSummary": "One year of specialized experience."})
    assert fed_reject(j) is False


def test_fed_reject_tolerates_a_missing_raw_payload():
    """Non-federal sources have no raw payload at all."""
    assert fed_reject(Job(description="Review annotation output.")) is False


def test_fed_bonus_only_fires_on_a_targeted_series():
    assert fed_bonus(Job(raw=federal("0343"))) == 12
    assert fed_bonus(Job(raw=federal("9999"))) == 0
    assert fed_bonus(Job(raw=None)) == 0


# --------------------------------------------------------------------------
# too_old
# --------------------------------------------------------------------------

def test_a_posting_past_the_age_limit_is_old():
    assert too_old(Job(posted_at=days_ago(MAX_AGE_DAYS + 5))) is True


def test_a_recent_posting_is_not_old():
    assert too_old(Job(posted_at=days_ago(1))) is False


def test_a_naive_timestamp_is_treated_as_utc_rather_than_raising():
    """Boards are inconsistent about timezone info. Subtracting a naive
    datetime from an aware one raises TypeError, which would kill the run."""
    assert too_old(Job(posted_at=days_ago(MAX_AGE_DAYS + 5, naive=True))) is True


def test_a_missing_date_is_not_treated_as_old():
    """Absent is not stale. Dropping undated postings would silently remove
    every source that doesn't publish a date."""
    assert too_old(Job(posted_at=None)) is False
