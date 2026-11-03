import pytest
import io
import zipfile
from fastapi.testclient import TestClient

from app import (
    app,
    sanitize_folder_name,
    estimate_tokens,
    get_text_stats,
    process_extracted_images,
    EXTRACTED_IMAGES_DIR,
)

client = TestClient(app)


def test_health_check():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["app_name"] == "MarkItDown Studio"
    assert data["version"] == "2.0.0"
    assert data["converter_available"] is True
    assert "pdf" in data["supported_formats"]
    assert "docx" in data["supported_formats"]
    assert "pptx" in data["supported_formats"]


def test_serve_home_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    assert "MarkItDown Studio" in html
    assert "rawMarkdownTextarea" in html
    assert "previewScrollContainer" in html
    assert "toggleSyncScroll" in html


def test_sanitize_folder_name():
    assert sanitize_folder_name("Normal Document") == "Normal_Document"
    assert sanitize_folder_name("../../../etc/passwd") == "passwd"
    assert sanitize_folder_name(r"..\..\Windows\System32") == "System32"
    assert sanitize_folder_name("!@#$%^&*()") == "document"
    assert sanitize_folder_name("presentation-2026.pptx") == "presentation-2026_pptx"


def test_estimate_tokens_and_stats():
    text = "Hello world! This is a test document with several words."
    tokens = estimate_tokens(text)
    assert tokens > 0

    stats = get_text_stats(text)
    assert stats["words"] == 10
    assert stats["characters"] == len(text)
    assert stats["lines"] == 1
    assert stats["reading_time_min"] == 1

    empty_stats = get_text_stats("")
    assert empty_stats["words"] == 0
    assert empty_stats["characters"] == 0


def test_convert_file_endpoint():
    content = b"# Test Document\n\nThis is sample content for MarkItDown conversion."
    files = {"file": ("sample.txt", io.BytesIO(content), "text/plain")}
    response = client.post("/api/convert-file", files=files, data={"image_mode": "local_disk"})

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "sample.txt" in data["filename"]
    assert "Test Document" in data["markdown"]
    assert data["stats"]["words"] > 0


def test_convert_files_batch_endpoint():
    file1 = ("doc1.txt", io.BytesIO(b"# Heading 1\nContent 1"), "text/plain")
    file2 = ("doc2.txt", io.BytesIO(b"# Heading 2\nContent 2"), "text/plain")

    response = client.post(
        "/api/convert-files",
        files=[("files", file1), ("files", file2)],
        data={"image_mode": "none"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["total_files"] == 2
    assert data["success_count"] == 2
    assert len(data["results"]) == 2


def test_convert_url_validation():
    # Invalid URL scheme
    response = client.post("/api/convert-url", data={"url": "ftp://example.com/file.pdf"})
    assert response.status_code == 400
    assert "URL không hợp lệ" in response.json()["detail"]

    response_empty = client.post("/api/convert-url", data={"url": "javascript:alert(1)"})
    assert response_empty.status_code == 400


def test_security_path_traversal_download_zip():
    # Attacking folder_name with traversal should be sanitized and confined safely
    response = client.post(
        "/api/download-zip",
        data={
            "folder_name": "../../../etc",
            "filename": "test_doc",
            "markdown_text": "# Sample Markdown Content"
        }
    )
    # Sanitization forces folder to 'etc', inside EXTRACTED_IMAGES_DIR, zip successfully generated safely
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    # Inspect zip contents
    zip_bytes = io.BytesIO(response.content)
    with zipfile.ZipFile(zip_bytes, "r") as zf:
        namelist = zf.namelist()
        assert "test_doc.md" in namelist
        for name in namelist:
            assert not name.startswith("/")
            assert ".." not in name


def test_process_extracted_images_modes():
    sample_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    raw_md = f"Sample text with image: ![Alt Text](data:image/png;base64,{sample_b64})"

    # Mode: none
    md_none, info_none = process_extracted_images(raw_md, "test_doc", image_mode="none")
    assert "data:image" not in md_none
    assert info_none["images_count"] == 0

    # Mode: ref_base64
    md_ref, info_ref = process_extracted_images(raw_md, "test_doc", image_mode="ref_base64")
    assert "![Alt Text][img_1]" in md_ref
    assert "<!-- BỘ NHỚ HÌNH ẢNH (IMAGE ASSETS)             -->" in md_ref
    assert info_ref["images_count"] == 1

    # Mode: local_disk
    md_disk, info_disk = process_extracted_images(raw_md, "test_doc", image_mode="local_disk")
    assert "data:image" not in md_disk
    assert "image_1.png" in md_disk
    assert info_disk["images_count"] == 1
    test_img = EXTRACTED_IMAGES_DIR / "test_doc" / "image_1.png"
    assert test_img.exists()

    # Clean up test artifact
    try:
        if test_img.exists():
            test_img.unlink()
        (EXTRACTED_IMAGES_DIR / "test_doc").rmdir()
    except Exception:
        pass
