# Table vision extraction template · v1
입력은 승인된보고서의 table/page crop과물리페이지정보다. 보이는값·header·unit·footnote·rowspan/colspan을 구조화하되, 읽을수없는값은null+unreadable로 기록한다. 원래숫자를고치거나 합계로빈값을추정하지 않는다. 화면의좌표와scale/transform을 함께 반환한다.
같은지표의열을연도에 정확히귀속시키고시장기반/위치기반·Scope·사업장을분리한다. 보고서전체가검증되었다고가정하지않는다. 차트의모양을보고소수점수치를창작하지않는다. 기존파서결과를정답으로따라쓰지않도록1차비전입력에는 파서가추출한숫자를넣지않는다. 비교는추출후코드에서수행한다.
