"""Exact quotes can locate spans; ambiguous or invented text cannot."""

import pytest

from evaluation.upstage_live_probe import locate_quotes


def test_exact_unique_quotes_receive_original_offsets():
    spans = locate_quotes({"claims": ["배출량을 줄였다."]}, "회사는 배출량을 줄였다.")
    assert (spans[0].char_start, spans[0].char_end, spans[0].quote) == (4, 13, "배출량을 줄였다.")


@pytest.mark.parametrize(
    "payload,text",
    [
        ({"claims": ["not there"]}, "original"),
        ({"claims": ["same"]}, "same same"),
        ({"claims": ["abc", "bc"]}, "abc"),
        ({"claims": [""]}, "original"),
        ({"claims": ["abc"], "grade": "E3"}, "abc"),
    ],
)
def test_untrusted_quotes_fail_closed(payload, text):
    with pytest.raises(ValueError):
        locate_quotes(payload, text)


@pytest.mark.parametrize(
    "text,quote",
    [
        (
            "두산밥캣은 KPI에 ‘2030 온실가스 감축 목표 달성을 위한 지역별",
            "두산밥캣은 KPI에 ‘2030 온실가스 감축 목표 달성을 위한 지역별",
        ),
        ("배출량 목표 수립’과 ‘2024년 대응 계획 이행’을 포함합니다.", "배출량 목표 수립"),
        ("당사는 “탄소 배출량을 줄였습니다.", "탄소 배출량을 줄였습니다."),
    ],
)
def test_claim_in_unclosed_source_quotation_is_unknown_not_a_complete_copy(text, quote):
    with pytest.raises(ValueError, match="unclosed source quotation"):
        locate_quotes({"claims": [quote]}, text)


def test_balanced_quotation_and_complete_sentence_before_cut_remain_eligible():
    text = "당사는 ‘2030 목표’를 수립했습니다. 다음 목표는 ‘지역별"
    quote = "당사는 ‘2030 목표’를 수립했습니다."
    assert locate_quotes({"claims": [quote]}, text)[0].quote == quote
    text = "O’Reilly reduced emissions."
    assert locate_quotes({"claims": [text]}, text)[0].quote == text
