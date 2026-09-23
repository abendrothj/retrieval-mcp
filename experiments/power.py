#!/usr/bin/env python3
"""What margin a suite of this size can actually test, before a registration promises one.

`runs/linux-scanfix-20260920` states the problem in its own `power` field: the native control
resolved 62 of 63, so the suite sat at its ceiling and no quality win was available to be
measured. Three earlier studies in this record reported "quality ties" from designs with that
same ceiling, and **a tie at ceiling is an absence of power, not evidence of equivalence**. That
field is prose, and it is `null` in two of the three preregistrations that have one.

This computes it. Given how many trials a study will run and how well the reference arm already
does, it returns the smallest non-inferiority margin the study could demonstrate, the headroom
that exists above the reference at all, and - when the positive control's ceiling from
`page_position.py` is supplied - whether the control is powered to show the suite can see
retrieval. Passing `--margin` turns it into a gate: a margin the design cannot test exits
nonzero, the way `validate_suite.py` refuses a suite that does not exist.

The test is the exact one-sided binomial, computed from `math.comb`, because the alternative is
adding a dependency to this project for an arithmetic it can do exactly. Trials are treated as
independent Bernoulli draws, which is the conservative reading: the arms answer the same
questions, so a paired test on discordant pairs would have *more* power, and a margin this
refuses might be reachable under McNemar. It never promises power the design does not have,
which is the direction that matters for a registration.

    power.py --questions 30 --repetitions 3 --control-rate 0.967 --at-risk 9

No model, no network, no corpus.
"""
import argparse
from functools import lru_cache
import json
import math
from pathlib import Path

# A registration's usual defaults. One-sided, because non-inferiority is a one-sided question.
ALPHA = 0.05
TARGET_POWER = 0.8
# Margins are searched on this grid. Finer than a single answer out of any suite this project
# runs, so the reported margin is limited by the design rather than by the search.
STEP = 0.001


@lru_cache(maxsize=None)
def survival(successes, trials, rate):
    """P(X >= successes) for X ~ Binomial(trials, rate). Exact, no dependency."""
    if successes <= 0:
        return 1.0
    if successes > trials:
        return 0.0
    return math.fsum(math.comb(trials, k) * rate ** k * (1 - rate) ** (trials - k)
                     for k in range(successes, trials + 1))


def critical_value(trials, null_rate, alpha):
    """The fewest successes that reject H0: rate <= null_rate at this one-sided alpha.

    `trials + 1` means no outcome rejects, which is a real answer for a small suite and is
    reported rather than smoothed away.
    """
    for successes in range(trials + 1):
        if survival(successes, trials, null_rate) <= alpha:
            return successes
    return trials + 1


def power_for(trials, control_rate, margin, alpha):
    """Chance of rejecting non-inferiority at this margin when the arms are truly equal."""
    null_rate = control_rate - margin
    if null_rate <= 0:
        return 1.0
    return survival(critical_value(trials, null_rate, alpha), trials, control_rate)


def reachable_margin(trials, control_rate, alpha=ALPHA, target=TARGET_POWER):
    """The smallest non-inferiority margin this design could demonstrate.

    Power rises with the margin, so this walks the grid upward and stops at the first margin that
    clears the target. None means no margin below the control rate reaches it.
    """
    margin = STEP
    while margin < control_rate:
        if power_for(trials, control_rate, margin, alpha) >= target:
            return round(margin, 4)
        margin += STEP
    return None


def headroom(trials, control_rate):
    """How much room exists above the reference arm at all, in trials and in rate.

    A study whose reference already resolves 62 of 63 cannot measure a quality win, whatever its
    sample size: the measurement is bounded by the ceiling, not by the statistics.
    """
    missed = trials * (1 - control_rate)
    return {"trials_the_control_loses": round(missed, 2),
            "rate_headroom": round(1 - control_rate, 4),
            "a_win_is_measurable": missed >= 2}


def control_floor(trials, control_rate, at_risk_trials, alpha=ALPHA, target=TARGET_POWER):
    """Is the positive control powered, given how many trials its knob can cost?

    `at_risk_trials` is `page_position.py`'s ceiling carried into trial counts: the most the
    degraded arm can lose. If that worst case is smaller than the margin this design can
    demonstrate, the control cannot show the suite sees retrieval even when it works perfectly,
    and the registration should say so before the run rather than after it.
    """
    if at_risk_trials is None:
        return None
    worst_case_rate = max(0.0, control_rate - at_risk_trials / trials)
    margin = reachable_margin(trials, control_rate, alpha, target)
    drop = at_risk_trials / trials
    return {
        "at_risk_trials": at_risk_trials,
        "worst_case_degraded_rate": round(worst_case_rate, 4),
        "largest_drop_the_knob_can_cause": round(drop, 4),
        "smallest_drop_this_design_can_show": margin,
        # Deliberately not a bare "powered". `page_position.py`'s at-risk count is a *ceiling* -
        # visibility is generous and a degraded arm recovers some questions by asking different
        # follow-ups - so a real drop is smaller than this and a ratio near 1 means the control
        # clears the bar only in its best case. The ratio is reported instead of a constant,
        # because what counts as enough head-room is a registration's judgement, not this
        # script's.
        "powered_at_the_ceiling": margin is not None and drop >= margin,
        "ceiling_to_margin_ratio": None if not margin else round(drop / margin, 2),
        "control_rejects_at_or_below": critical_value(trials, worst_case_rate, alpha),
    }


def report(questions, repetitions, control_rate, at_risk=None, alpha=ALPHA,
           target=TARGET_POWER):
    trials = questions * repetitions
    margin = reachable_margin(trials, control_rate, alpha, target)
    at_risk_trials = None if at_risk is None else at_risk * repetitions
    return {
        "version": "power-v1",
        "questions": questions,
        "repetitions": repetitions,
        "trials": trials,
        "control_rate": control_rate,
        "alpha_one_sided": alpha,
        "target_power": target,
        "reachable_non_inferiority_margin": margin,
        "reachable_margin_in_trials": None if margin is None else round(margin * trials, 2),
        "headroom": headroom(trials, control_rate),
        "positive_control": control_floor(trials, control_rate, at_risk_trials, alpha, target),
        "method": "Exact one-sided binomial on independent trials. The arms answer the same "
                  "questions, so a paired test on discordant pairs would have more power and a "
                  "margin refused here may be reachable under McNemar; this never promises power "
                  "the design does not have, which is the direction a registration needs.",
    }


def render(result):
    margin, trials = result["reachable_non_inferiority_margin"], result["trials"]
    lines = [f"{result['questions']} questions x {result['repetitions']} repetitions "
             f"= {trials} trials, control rate {result['control_rate']:.3f}",
             f"  smallest testable non-inferiority margin: "
             + (f"{margin:.1%} ({result['reachable_margin_in_trials']} trials)" if margin
                else f"none below the control rate at power {result['target_power']:.0%}")]
    room = result["headroom"]
    lines.append(f"  headroom above the control: {room['trials_the_control_loses']} trials "
                 f"({room['rate_headroom']:.1%})"
                 + ("" if room["a_win_is_measurable"]
                    else "  <- at ceiling: no quality win is measurable here"))
    floor = result["positive_control"]
    if floor:
        verdict = (f"clears the margin only at its ceiling, by "
                   f"{floor['ceiling_to_margin_ratio']}x - and the ceiling overstates"
                   if floor["powered_at_the_ceiling"]
                   else "NOT powered - even its best case is smaller than the margin")
        lines.append(f"  positive control can cost at most {floor['at_risk_trials']} trials "
                     f"({floor['largest_drop_the_knob_can_cause']:.1%}); {verdict}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--questions", type=int, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--control-rate", type=float, required=True,
                        help="the reference arm's per-trial success rate, e.g. 62/63")
    parser.add_argument("--at-risk", type=int, default=None,
                        help="questions a positive control can cost, from page_position.py")
    parser.add_argument("--margin", type=float, default=None,
                        help="a proposed margin; exits nonzero when the design cannot test it")
    parser.add_argument("--alpha", type=float, default=ALPHA)
    parser.add_argument("--power", type=float, default=TARGET_POWER)
    parser.add_argument("--output", type=Path, default=None,
                        help="write the block to paste into the preregistration's `power` field")
    args = parser.parse_args()
    if not 0 < args.control_rate <= 1:
        parser.error("--control-rate is a rate in (0, 1]")
    result = report(args.questions, args.repetitions, args.control_rate, args.at_risk,
                    args.alpha, args.power)
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(render(result))
    if args.margin is not None:
        reachable = result["reachable_non_inferiority_margin"]
        if reachable is None or args.margin < reachable:
            print(f"\nREFUSED: a margin of {args.margin:.1%} is not testable by this design; the "
                  f"smallest it can demonstrate is "
                  + (f"{reachable:.1%}." if reachable else "none."))
            return 1
        print(f"\nOK: {args.margin:.1%} is testable (smallest is {reachable:.1%}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
