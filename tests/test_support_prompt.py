"""The Support prompt rules that h03 and x04 depend on. The behavior itself is measured by the eval (h03, x04, h01, h02, 5 repeats each);
these pin down the wording so a later edit cannot silently widen rule 3 again."""
import re

from app.prompts import SUPPORT_PROMPT


def rule(n: int) -> str:
    m = re.search(rf"^{n}\. (.*?)(?=^\d\. |\Z|\{{prior_context\}})", SUPPORT_PROMPT, re.S | re.M)
    assert m, n
    return m.group(1)


def test_rule_3_applies_only_when_the_customer_explicitly_asks_for_another_customers_data():
    r3 = rule(3)
    assert "ONLY when the customer explicitly asks for data of ANOTHER customer" in r3
    assert "not this case: it goes to rule 7" in r3 and "do NOT call escalate_to_human for this" in r3


def test_rule_7_sends_the_unanswerable_question_to_a_human_instead_of_a_refusal():
    r7 = rule(7)
    assert "a loan, a contract" in r7 and "must be escalated here" in r7 and "I can't share that" in r7


def test_the_prompt_still_formats_with_its_three_placeholders_and_nothing_else():
    out = SUPPORT_PROMPT.format(user_id="cliente1988", language="English", prior_context="")
    assert "cliente1988" in out and "{" not in out and "}" not in out
