"""Checks that tie a submission to its contract, its ledger and its approval.

Used in two places:

- **At `/validate` and `/submit`** (`scripts.check_submission`): the parsed
  issue is checked against `contracts/<id>.yml` and `ledger/<id>.yml`.
  Problems come back to the contractor as an ordinary "needs a fix" comment,
  the same as a parse error, instead of a generic pipeline failure.

- **At merge** (`scripts.verify_approval`): before anything is stamped,
  ledgered or emailed, the engine checks who merged, which files the PR
  changed, and recomputes every total from the entries and the contract on
  `main`. The PR branch is editable, so the totals in its YAML are a claim,
  not a fact; approval must not trust them.

All functions here are pure: they take dicts and return lists of messages.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

from scripts.money import (
    amount_for_hours,
    format_money,
    has_excess_precision,
    minor_units,
    quantize_hours,
    quantize_money,
    sum_decimal,
    to_decimal,
)

# Contract IDs become file paths (`contracts/<id>.yml`, `ledger/<id>.yml`),
# so anything that could leave the directory or break a shell/env line is
# refused before it is used. Covers the `QE-PSL-2026-001` convention.
SAFE_CONTRACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# A submission PR may only change engine-generated artifacts.
ALLOWED_PR_PATH_PREFIXES = ("submissions/", "generated_pdfs/")

_CONTRACT_TYPE_FOR_SUBMISSION = {
    "timesheet": "hourly",
    "milestone_invoice": "milestone",
}
_FORM_FOR_CONTRACT_TYPE = {
    "hourly": "Hourly Timesheet",
    "milestone": "Milestone Invoice",
}


def is_safe_contract_id(contract_id: object) -> bool:
    return isinstance(contract_id, str) and bool(SAFE_CONTRACT_ID.match(contract_id))


def contract_currency(contract: dict) -> Optional[str]:
    """Hourly contracts keep currency under `terms`; milestone contracts at the top."""
    if contract.get("type") == "hourly":
        return (contract.get("terms") or {}).get("currency")
    return contract.get("currency")


def _as_date(value: object) -> Optional[date]:
    """YAML gives `date` objects for bare dates; JSON/strings give ISO text."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _window_text(start: Optional[date], end: Optional[date]) -> str:
    return f"{start.isoformat() if start else '(no start)'} to {end.isoformat() if end else '(open-ended)'}"


def _outside_window(dates: Iterable[object], start: Optional[date], end: Optional[date]) -> list[str]:
    outside = []
    for raw in dates:
        d = _as_date(raw)
        if d is None:
            continue
        if (start and d < start) or (end and d > end):
            outside.append(d.isoformat())
    return outside


def _paid_milestones(ledger: Optional[dict], exclude: set[str]) -> dict[str, dict]:
    """Milestone ID -> the active ledger claim that paid it."""
    paid: dict[str, dict] = {}
    for claim in (ledger or {}).get("claims") or []:
        if claim.get("status") == "superseded":
            continue
        if claim.get("submission_id") in exclude:
            continue
        for entry in claim.get("entries") or []:
            paid.setdefault(str(entry.get("id")), claim)
    return paid


def check_against_contract(
    submission: dict,
    contract: dict,
    *,
    ledger: Optional[dict] = None,
    exclude_submission_ids: Iterable[str] = (),
    check_status: bool = True,
) -> tuple[list[str], list[str]]:
    """Check a submission's entries against its contract and ledger.

    Returns (errors, warnings). Errors block; warnings are shown to the admin
    on the PR (and to the contractor on `/validate`).

    `exclude_submission_ids` names ledger claims that don't count as prior
    payments: the submission this one supersedes (a revision legitimately
    re-claims its milestones) and the submission itself (so a re-run after the
    ledger was updated doesn't flag it against its own entry).

    `check_status` is on at submit time only. An admin may mark a contract
    `ended` after its last claim is filed but before it is approved; that
    shouldn't block the approval.
    """
    errors: list[str] = []
    warnings: list[str] = []

    contract_id = submission.get("contract_id")
    submission_type = submission.get("type")
    expected_type = _CONTRACT_TYPE_FOR_SUBMISSION.get(submission_type)
    contract_type = contract.get("type")

    if expected_type and contract_type != expected_type:
        right_form = _FORM_FOR_CONTRACT_TYPE.get(contract_type, "matching")
        this_form = _FORM_FOR_CONTRACT_TYPE.get(expected_type, "this")
        errors.append(
            f"Contract `{contract_id}` is a {contract_type} contract, so it can't be "
            f"claimed on the {this_form} form. Use the {right_form} form instead, "
            f"or pick a different contract."
        )
        return errors, warnings

    if check_status and contract.get("status") == "ended":
        errors.append(
            f"Contract `{contract_id}` has ended (`status: ended`), so it can't take "
            f"new claims. If this work was done under it, ask your administrator."
        )

    start = _as_date(contract.get("start_date"))
    end = _as_date(contract.get("end_date"))
    entries = submission.get("entries") or []

    if submission_type == "timesheet":
        outside = _outside_window((e.get("date") for e in entries), start, end)
        if outside:
            errors.append(
                f"{len(outside)} date{'s' if len(outside) != 1 else ''} fall outside "
                f"contract `{contract_id}` ({_window_text(start, end)}): "
                f"{', '.join(outside)}. Only work done during the contract can be "
                f"claimed on it."
            )
        return errors, warnings

    if submission_type == "milestone_invoice":
        currency = contract_currency(contract) or ""

        if currency:
            for e in entries:
                if has_excess_precision(e["amount"], currency):
                    places = minor_units(currency)
                    errors.append(
                        f"Milestone `{e['id']}`: amount `{e['amount']}` has more decimal "
                        f"places than {currency} allows ({places}). Enter the exact "
                        f"amount, e.g. `{format_money(e['amount'], currency).replace(',', '')}`."
                    )

        schedule = {
            str(m["id"]): m
            for m in (contract.get("milestones") or [])
            if isinstance(m, dict) and "id" in m
        }
        for e in entries:
            scheduled = schedule.get(str(e.get("id")))
            if scheduled is None or scheduled.get("amount") is None or not currency:
                continue  # unknown IDs are flagged by cross_check_milestone_ids
            if to_decimal(scheduled["amount"]) != to_decimal(e["amount"]):
                warnings.append(
                    f"Milestone `{e['id']}`: claimed "
                    f"{format_money(e['amount'], currency)} {currency}, but contract "
                    f"`{contract_id}` schedules "
                    f"{format_money(scheduled['amount'], currency)} {currency}. "
                    f"Admin: confirm the difference before merging."
                )

        # Late delivery and catch-up claims are legitimate, so dates outside
        # the window only warn for milestones.
        outside = _outside_window((e.get("date") for e in entries), start, end)
        if outside:
            warnings.append(
                f"Milestone date{'s' if len(outside) != 1 else ''} "
                f"{', '.join(outside)} fall outside contract `{contract_id}` "
                f"({_window_text(start, end)}). Admin: confirm before merging."
            )

        paid = _paid_milestones(ledger, set(exclude_submission_ids))
        for e in entries:
            claim = paid.get(str(e.get("id")))
            if claim is None:
                continue
            approved = claim.get("approved_date")
            errors.append(
                f"Milestone `{e['id']}` has already been paid on "
                f"`{claim.get('submission_id')}`"
                f"{f' (approved {approved})' if approved else ''}. Each milestone can "
                f"only be claimed once. To correct that invoice, reopen its issue and "
                f"`/submit` a revision instead."
            )

    return errors, warnings


def verify_totals(submission: dict, contract: dict) -> list[str]:
    """Recompute a submission's totals from its entries and the contract.

    Every way the committed YAML can disagree with what the engine would have
    generated is an error: hand-edited totals, a changed rate, entries outside
    the period, duplicate days. Merge-time only.
    """
    errors: list[str] = []
    contract_id = submission.get("contract_id")
    if contract.get("contract_id") != contract_id:
        return [
            f"Submission says contract `{contract_id}` but the contract file says "
            f"`{contract.get('contract_id')}`."
        ]

    submission_type = submission.get("type")
    expected_type = _CONTRACT_TYPE_FOR_SUBMISSION.get(submission_type)
    if expected_type is None:
        return [f"Unknown submission type `{submission_type}`."]
    if contract.get("type") != expected_type:
        return [
            f"Submission type `{submission_type}` needs a `{expected_type}` contract, "
            f"but `{contract_id}` is `{contract.get('type')}`."
        ]

    currency = contract_currency(contract)
    totals = submission.get("totals") or {}
    entries = submission.get("entries") or []

    if not currency:
        return [f"Contract `{contract_id}` has no currency."]
    if totals.get("currency") != currency:
        errors.append(
            f"Currency is `{totals.get('currency')}` but contract `{contract_id}` "
            f"is in `{currency}`."
        )
    if not entries:
        errors.append("The submission has no entries.")
        return errors

    try:
        if submission_type == "timesheet":
            errors.extend(_verify_timesheet(submission, contract, currency, entries, totals))
        else:
            errors.extend(_verify_milestones(currency, entries, totals))
    except (KeyError, TypeError, ValueError) as e:
        errors.append(f"The submission YAML is malformed and can't be verified: {e!r}.")
    return errors


def _verify_timesheet(submission, contract, currency, entries, totals) -> list[str]:
    errors: list[str] = []
    period = str(submission.get("period", ""))
    seen: set[str] = set()
    for e in entries:
        hours = to_decimal(e["hours"])
        d = _as_date(e["date"])
        label = e["date"] if d is None else d.isoformat()
        if d is None:
            errors.append(f"Entry date `{e['date']}` isn't a valid date.")
        elif d.strftime("%Y-%m") != period:
            errors.append(f"Entry `{label}` is outside the period `{period}`.")
        if label in seen:
            errors.append(f"Entry date `{label}` appears more than once.")
        seen.add(label)
        if hours <= 0 or hours > 24:
            errors.append(f"Entry `{label}`: hours `{e['hours']}` must be more than 0 and at most 24.")
        elif hours != quantize_hours(hours):
            errors.append(f"Entry `{label}`: hours `{e['hours']}` have more than two decimal places.")

    hours_sum = sum_decimal(e["hours"] for e in entries)
    if to_decimal(totals["hours"]) != quantize_hours(hours_sum):
        errors.append(
            f"Total hours are `{totals['hours']}` but the entries add up to "
            f"`{quantize_hours(hours_sum)}`."
        )

    rate = (contract.get("terms") or {}).get("hourly_rate")
    if rate is None:
        errors.append(f"Contract `{contract.get('contract_id')}` has no hourly rate.")
        return errors
    if to_decimal(totals["rate"]) != quantize_money(rate, currency):
        errors.append(
            f"Rate is `{totals['rate']}` but contract `{contract.get('contract_id')}` "
            f"says `{format_money(rate, currency)}` {currency}/hour."
        )
    expected = amount_for_hours(hours_sum, rate, currency)
    if to_decimal(totals["amount"]) != expected:
        errors.append(
            f"Amount is `{totals['amount']}` {currency} but "
            f"{quantize_hours(hours_sum)} hours x {format_money(rate, currency)} "
            f"{currency} = {format_money(expected, currency)} {currency}."
        )
    return errors


def _verify_milestones(currency, entries, totals) -> list[str]:
    errors: list[str] = []
    for e in entries:
        amount = to_decimal(e["amount"])
        if amount <= 0:
            errors.append(f"Milestone `{e['id']}`: amount must be greater than 0.")
        elif has_excess_precision(amount, currency):
            errors.append(
                f"Milestone `{e['id']}`: amount `{e['amount']}` has more decimal "
                f"places than {currency} allows."
            )
    expected = quantize_money(sum_decimal(e["amount"] for e in entries), currency)
    if to_decimal(totals["amount"]) != expected:
        errors.append(
            f"Total is `{totals['amount']}` {currency} but the milestones add up to "
            f"{format_money(expected, currency)} {currency}."
        )
    return errors


def verify_merge(
    changed_files: Iterable[str],
    merged_by: str,
    approvers: Iterable[str],
) -> list[str]:
    """Check who merged a submission PR and what it changed."""
    errors: list[str] = []
    approver_list = [a for a in approvers if a]
    if not approver_list:
        errors.append(
            "No approvers are configured (`quantecon.approvers` in the engine's "
            "`templates/fiscal-host.yml`), so no merge can count as an approval."
        )
    elif (merged_by or "").lower() not in {a.lower() for a in approver_list}:
        allowed = ", ".join(f"@{a}" for a in approver_list)
        errors.append(
            f"@{merged_by} merged this PR but is not an authorised approver "
            f"({allowed}). A submission is approved only when an authorised "
            f"approver merges it."
        )

    outside = sorted({
        f for f in (p.strip() for p in changed_files)
        if f and not f.startswith(ALLOWED_PR_PATH_PREFIXES)
    })
    if outside:
        errors.append(
            "This PR changes files outside `submissions/` and `generated_pdfs/`: "
            + ", ".join(f"`{f}`" for f in outside)
            + ". Contracts, settings and workflows can't change inside a "
            "submission PR."
        )
    return errors
