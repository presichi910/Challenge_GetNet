import pytest

from app.lang import detect_language, same_language

QUESTIONS = [
    ("What's the weather forecast in Porto Alegre tomorrow?", "en"),
    ("What's the euro exchange rate today?", "en"),
    ("Which of the two days was bigger?", "en"),
    ("I want to talk to a human agent.", "en"),
    ("Hi there!", "en"),
    ("Write me a short poem about football.", "en"),
    ("Can you say that again in simpler words?", "en"),
    ("Quando vou receber o dinheiro das vendas de ontem?", "pt"),
    ("Quero falar com um atendente, por favor.", "pt"),
    ("Necesito hablar con una persona sobre un cobro que no reconozco.", "es"),
    ("Hola, ¿cómo puedo ver mis ventas de hoy?", "es"),
]


@pytest.mark.parametrize("text,lang", QUESTIONS)
def test_the_questions_of_the_eval_are_detected(text, lang):
    assert detect_language(text) == lang


def test_a_text_too_short_or_without_language_words_is_unknown():
    assert detect_language("Pix") is None
    assert detect_language("") is None
    assert detect_language("cliente2024 TKT-08B75F") is None


def test_a_portuguese_term_inside_an_english_answer_does_not_flip_it():
    text = ("Receivables advance (antecipação de recebíveis) lets you receive today the money of sales "
            "that would be paid later, for a fee. The antecipação is requested in the app.")
    assert detect_language(text) == "en"


def test_a_portuguese_answer_to_an_english_question_is_a_mismatch():
    assert same_language("Which of the two days was bigger?",
                         "Você teve mais vendas hoje: 450 reais, contra 300 reais ontem.") is False


def test_same_language_is_true_when_both_agree_and_none_when_unsure():
    assert same_language("How many installments can I use?", "You can split a sale into up to 48 installments.") is True
    assert same_language("Pix", "You can split a sale into up to 48 installments.") is None
