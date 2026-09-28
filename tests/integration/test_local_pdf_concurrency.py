"""The local API must not overlap native PDF access across HTTP requests."""

import asyncio
import threading
from contextlib import closing

import httpx
import pytest

from tests.acceptance.test_upload import pdf


def test_local_api_serializes_native_pdf_requests_and_releases_on_error(tmp_path, monkeypatch):
    from proofops_api.main import create_app

    monkeypatch.setenv("LOCAL_DATABASE_PATH", str(tmp_path / "state.sqlite3"))
    app = create_app()
    source = pdf(3)
    entered, release = threading.Event(), threading.Event()
    counter_lock = threading.Lock()
    active = peak = 0

    @app.get("/native-pdf-check")
    def native_read():
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(active, peak)
        try:
            entered.set()
            assert release.wait(2)
            # Detect overlap before risking a real PDFium segfault in the test process.
            if peak > 1:
                return {"overlapped": True}
            import pypdfium2 as pdfium

            with pdfium.PdfDocument(source) as document, closing(document[0]) as page:
                assert page.get_width() > 0  # Exercise FPDF_LoadPage, the observed crash site.
                return {"pages": len(document)}
        finally:
            with counter_lock:
                active -= 1

    @app.get("/native-pdf-error")
    def failure():
        raise ValueError("test failure must release admission")

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://local.test",
        ) as client:
            first = asyncio.create_task(client.get("/native-pdf-check"))
            assert await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(client.get("/native-pdf-check"))
            await asyncio.sleep(0.05)
            release.set()
            responses = await asyncio.gather(first, second)
            assert peak == 1, "native PDF access overlapped across local API requests"
            assert [r.json() for r in responses] == [{"pages": 3}, {"pages": 3}]
            assert (await client.get("/native-pdf-error")).status_code == 500
            assert (await client.get("/native-pdf-check")).json() == {"pages": 3}

            # Cancelling the HTTP task must not release admission while its
            # native worker is still running (asyncio cancellation cannot stop C).
            entered.clear()
            release.clear()
            cancelled = asyncio.create_task(client.get("/native-pdf-check"))
            assert await asyncio.to_thread(entered.wait, 2)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            following = asyncio.create_task(client.get("/native-pdf-check"))
            await asyncio.sleep(0.05)
            release.set()
            assert (await following).json() == {"pages": 3}
            assert peak == 1

    asyncio.run(check())
