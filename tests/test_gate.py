"""Tests for the hard gate.

The gate returns None when a posting passes, or a string saying why it was
rejected. These tests pin that behaviour so a change to the pattern lists
cannot silently start dropping roles.

Two of them are regressions for bugs that reached production and were found
by reading rejection logs, not by testing.
"""
import pytest
from fitscore import hard_gate


# ---- passes ----

def test_rescue_word_saves_a_blocked_title():
    """'staff ' is a blocked title word, but 'quality' rescues it. This is
    what keeps the gate rejecting by function rather than by vocabulary."""
    assert hard_gate("Staff AI Quality Engineer", "") is None


def test_ordinary_target_role_passes():
    assert hard_gate("Quality Assurance Specialist", "Review annotation output.") is None


def test_stack_mentioned_without_requirement_framing_passes():
    """A language named in a company blurb is not a requirement."""
    assert hard_gate("Quality Analyst", "Our platform is written in Rust.") is None


# ---- rejects ----

def test_blocked_title_without_rescue_is_rejected():
    r = hard_gate("Backend Engineer", "")
    assert r is not None and "title:" in r


def test_seniority_above_band_is_rejected():
    r = hard_gate("VP of Engineering", "")
    assert r is not None and "seniority:" in r


def test_publication_requirement_is_rejected():
    r = hard_gate("Quality Analyst", "Must have published at ICLR.")
    assert r is not None and "hard requirement:" in r


def test_required_stack_is_rejected():
    r = hard_gate("Quality Analyst", "Proficient in TypeScript required.")
    assert r is not None and "required stack:" in r


def test_clearance_requirement_is_rejected():
    r = hard_gate("Program Analyst", "Requires an active TS/SCI clearance.")
    assert r is not None and "hard requirement:" in r


# ---- regressions ----

def test_go_used_as_a_verb_is_not_a_language_requirement():
    """REGRESSION. The stack pattern matched a bare \\bgo\\b and the
    requirement pattern included 'deep', so 'go deep' parsed as a Go-language
    requirement. It rejected Security Risk Analyst at Anthropic. 'go to
    market' and 'go live' would have gone the same way."""
    assert hard_gate("Security Risk Analyst", "We go deep on risk engineering.") is None


def test_federal_education_substitution_is_not_a_degree_requirement():
    """REGRESSION. Federal postings list a doctorate as one qualifying path
    in the education-substitution block. The pattern matched the bare
    mention, dropping four on-target federal roles in a single run."""
    text = ("Education: Ph.D. or equivalent doctoral degree, OR one year of "
            "specialized experience equivalent to the GS-11 level.")
    assert hard_gate("Quality Assurance Specialist", text) is None


def test_a_genuinely_required_doctorate_is_still_rejected():
    """The fix must not have disarmed the rule entirely."""
    r = hard_gate("Quality Analyst", "Requires a Ph.D. in a quantitative field.")
    assert r is not None and "hard requirement:" in r


# ---- contract ----

@pytest.mark.parametrize("title,desc", [("", ""), (None, None), ("Quality Analyst", None)])
def test_gate_tolerates_missing_fields(title, desc):
    """Scrapers return partial records. The gate must not raise."""
    hard_gate(title, desc)
