"""A bounded preview must spend its first calls on prose, retaining short candidates."""

from types import SimpleNamespace

from proofops_worker.extract_runner import select_stable_paragraph_sources


def test_prose_precedes_navigation_without_excluding_short_assertions():
    texts = [
        "SUSTAINABILITY REPORT 2025 Introduction Our Approach "
        "Material Issues ESG Management Appendix 27",
        "환경영향 저감제품 지속가능성 기후변화 대응",
        "UN SDGs",
        "당사는 생산 과정에서 발생하는 온실가스 배출량을 줄이기 위해 "
        "사업장별 에너지 사용량을 관리하며 매년 감축 활동의 이행 결과를 점검하고 있습니다.",
        "배출량 10% 감축",
    ]
    blocks = [
        SimpleNamespace(
            source_id=str(i),
            page_num=27,
            kind="paragraph",
            quality="unverified",
            winner=0,
            bbox=(0, i * 20, 300, i * 20 + 15),
            normalized_text=text,
        )
        for i, text in enumerate(texts)
    ]
    graph = SimpleNamespace(blocks=blocks)
    scope = SimpleNamespace(mode="declared_subset", selected_pages=(27,))
    assert select_stable_paragraph_sources(graph, scope, 1) == {"3"}
    assert select_stable_paragraph_sources(graph, scope, 5) == {"0", "1", "2", "3", "4"}
    scope.selected_pages = (28,)
    assert select_stable_paragraph_sources(graph, scope, 4) == set()
