import os
import sys
import io
import json
import time
import socket
import tempfile
import webbrowser
import threading
import re
import base64
import zipfile
import math
from pathlib import Path
from typing import Optional, List

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from markitdown import MarkItDown

# Directory where extracted images are permanently saved
BASE_DIR = Path(__file__).resolve().parent
EXTRACTED_IMAGES_DIR = BASE_DIR / "extracted_images"
EXTRACTED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# Initialize MarkItDown
try:
    md_converter = MarkItDown()
except Exception as e:
    print(f"Error initializing MarkItDown: {e}", file=sys.stderr)
    md_converter = None

app = FastAPI(
    title="MarkItDown Studio",
    version="2.0.0",
    description="Dual Sync Scroll Document Converter & Inspection Studio",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount extracted images folder as static route so web preview can display them seamlessly
app.mount(
    "/extracted_images",
    StaticFiles(directory=str(EXTRACTED_IMAGES_DIR)),
    name="extracted_images",
)


def sanitize_folder_name(name: str) -> str:
    """Sanitize folder or document stem names to prevent path traversal and unsafe characters."""
    clean = re.sub(r"[^\w\-]", "_", Path(name).name).strip("_")
    return clean or "document"


def safe_resolve_image_folder(folder_name: str) -> Path:
    """Resolve a document image folder safely under EXTRACTED_IMAGES_DIR."""
    clean_name = sanitize_folder_name(folder_name)
    resolved = (EXTRACTED_IMAGES_DIR / clean_name).resolve()
    base_resolved = EXTRACTED_IMAGES_DIR.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise HTTPException(status_code=400, detail="Path traversal detected.")
    return resolved


def estimate_tokens(text: str) -> int:
    """Rough estimation of LLM tokens based on word and character counts."""
    if not text:
        return 0
    words = len(text.split())
    return max(int(words * 1.3), int(len(text) / 3.5))


def get_text_stats(text: str):
    if not text:
        return {
            "characters": 0,
            "words": 0,
            "lines": 0,
            "estimated_tokens": 0,
            "reading_time_min": 0,
        }
    lines = text.splitlines()
    words = text.split()
    word_count = len(words)
    return {
        "characters": len(text),
        "words": word_count,
        "lines": len(lines),
        "estimated_tokens": estimate_tokens(text),
        "reading_time_min": max(1, math.ceil(word_count / 200)) if word_count else 0,
    }


def process_extracted_images(
    markdown_text: str, doc_name: str, image_mode: str = "local_disk"
):
    """
    Handles images embedded as base64 in MarkItDown output:
    - 'local_disk': Saves images to extracted_images/<folder>/image_X.png.
                    Zero base64 tokens inside Markdown, letting AI inspect images on disk.
    - 'ref_base64': Body references ![alt][img_X], definitions appended at bottom.
    - 'base64': Keeps inline base64 data URIs intact.
    - 'none': Strips image data.
    """
    if not markdown_text:
        return markdown_text, {
            "image_mode": image_mode,
            "images_count": 0,
            "folder_path": None,
            "folder_name": None,
            "saved_images": [],
        }

    md_b64_pattern = re.compile(
        r"!\[([^\]]*)\]\(data:image/([a-zA-Z0-9\+\-]+);base64,([A-Za-z0-9+/=]+)\)"
    )
    matches = list(md_b64_pattern.finditer(markdown_text))

    if not matches:
        return markdown_text, {
            "image_mode": image_mode,
            "images_count": 0,
            "folder_path": None,
            "folder_name": None,
            "saved_images": [],
        }

    if image_mode == "base64":
        return markdown_text, {
            "image_mode": "base64",
            "images_count": len(matches),
            "folder_path": None,
            "folder_name": None,
            "saved_images": [],
        }

    if image_mode == "none":
        clean_text = md_b64_pattern.sub(r"![\1](image.png)", markdown_text)
        return clean_text, {
            "image_mode": "none",
            "images_count": 0,
            "folder_path": None,
            "folder_name": None,
            "saved_images": [],
        }

    clean_folder_name = sanitize_folder_name(doc_name)
    doc_img_dir = safe_resolve_image_folder(clean_folder_name)
    doc_img_dir.mkdir(parents=True, exist_ok=True)

    saved_images = []
    counter = 0

    if image_mode == "ref_base64":
        image_definitions = []

        def ref_replacer(match):
            nonlocal counter
            counter += 1
            alt_text = match.group(1).strip() or f"Ảnh {counter}"
            mime_subtype = match.group(2).lower()
            b64_str = match.group(3)

            ext = ".png"
            if mime_subtype in ("jpeg", "jpg"):
                ext = ".jpg"
            elif mime_subtype == "svg+xml":
                ext = ".svg"
            elif mime_subtype == "gif":
                ext = ".gif"
            elif mime_subtype == "webp":
                ext = ".webp"

            img_filename = f"image_{counter}{ext}"
            target_disk_path = doc_img_dir / img_filename
            try:
                img_bytes = base64.b64decode(b64_str)
                with open(target_disk_path, "wb") as f:
                    f.write(img_bytes)
                saved_images.append(img_filename)
            except Exception as err:
                print(f"Error saving image {img_filename}: {err}", file=sys.stderr)

            ref_id = f"img_{counter}"
            image_definitions.append(
                f"[{ref_id}]: data:image/{mime_subtype};base64,{b64_str}"
            )
            return f"![{alt_text}][{ref_id}]"

        body_text = md_b64_pattern.sub(ref_replacer, markdown_text)
        assets_section = (
            "\n\n\n<!-- ========================================== -->\n"
            "<!-- BỘ NHỚ HÌNH ẢNH (IMAGE ASSETS)             -->\n"
            "<!-- ========================================== -->\n"
            + "\n".join(image_definitions)
            + "\n"
        )
        cleaned_markdown = body_text + assets_section

        return cleaned_markdown, {
            "image_mode": "ref_base64",
            "images_count": counter,
            "folder_path": str(doc_img_dir),
            "folder_name": clean_folder_name,
            "saved_images": saved_images,
        }

    # 'local_disk' mode
    def disk_replacer(match):
        nonlocal counter
        counter += 1
        alt_text = match.group(1).strip() or f"Ảnh {counter}"
        mime_subtype = match.group(2).lower()
        b64_str = match.group(3)

        ext = ".png"
        if mime_subtype in ("jpeg", "jpg"):
            ext = ".jpg"
        elif mime_subtype == "svg+xml":
            ext = ".svg"
        elif mime_subtype == "gif":
            ext = ".gif"
        elif mime_subtype == "webp":
            ext = ".webp"

        img_filename = f"image_{counter}{ext}"
        target_disk_path = doc_img_dir / img_filename
        try:
            img_bytes = base64.b64decode(b64_str)
            with open(target_disk_path, "wb") as f:
                f.write(img_bytes)
            saved_images.append(img_filename)
        except Exception as err:
            print(f"Error saving image {img_filename}: {err}", file=sys.stderr)

        local_md_url = (doc_img_dir / img_filename).as_posix()
        return f"![{alt_text}]({local_md_url})"

    cleaned_markdown = md_b64_pattern.sub(disk_replacer, markdown_text)
    return cleaned_markdown, {
        "image_mode": "local_disk",
        "images_count": counter,
        "folder_path": str(doc_img_dir),
        "folder_name": clean_folder_name,
        "saved_images": saved_images,
    }


def convert_single_file(
    file_content: bytes, original_filename: str, image_mode: str = "local_disk"
) -> dict:
    """Helper to convert single file content into Markdown and process extracted images."""
    if md_converter is None:
        raise RuntimeError("MarkItDown converter is not initialized.")

    start_time = time.perf_counter()
    file_ext = Path(original_filename).suffix.lower()

    with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_file:
        temp_path = temp_file.name
        temp_file.write(file_content)
        temp_file.flush()

    try:
        want_extract = image_mode != "none"
        result = md_converter.convert(temp_path, keep_data_uris=want_extract)
        raw_markdown = result.text_content or ""
        doc_title = result.title or Path(original_filename).stem

        markdown_text, image_info = process_extracted_images(
            raw_markdown, doc_name=original_filename, image_mode=image_mode
        )

        duration_ms = round((time.perf_counter() - start_time) * 1000, 1)
        stats = get_text_stats(markdown_text)

        return {
            "success": True,
            "filename": original_filename,
            "title": doc_title,
            "markdown": markdown_text,
            "file_size": len(file_content),
            "duration_ms": duration_ms,
            "stats": stats,
            "image_info": image_info,
            "has_embedded_images": image_info["images_count"] > 0,
            "embedded_images_count": image_info["images_count"],
        }
    except Exception as e:
        return {
            "success": False,
            "filename": original_filename,
            "title": Path(original_filename).stem,
            "markdown": "",
            "file_size": len(file_content),
            "duration_ms": round((time.perf_counter() - start_time) * 1000, 1),
            "error": str(e),
            "hint": "Định dạng file có thể chưa có module chuyển đổi tương ứng hoặc nội dung bị mã hóa bảo vệ.",
            "stats": get_text_stats(""),
            "image_info": {"images_count": 0, "saved_images": []},
            "has_embedded_images": False,
            "embedded_images_count": 0,
        }
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "app_name": "MarkItDown Studio",
        "version": "2.0.0",
        "converter_available": md_converter is not None,
        "extracted_images_dir": str(EXTRACTED_IMAGES_DIR),
        "multi_file_support": True,
        "sync_scroll_support": True,
        "supported_formats": [
            "pdf",
            "docx",
            "pptx",
            "xlsx",
            "xls",
            "html",
            "htm",
            "txt",
            "csv",
            "json",
            "xml",
            "mp3",
            "wav",
            "m4a",
            "zip",
        ],
    }


@app.post("/api/convert-file")
async def convert_file(
    file: UploadFile = File(...),
    image_mode: str = Form("local_disk"),
    embed_images: Optional[bool] = Form(None),
):
    if embed_images is False and image_mode in ("ref_base64", "local_disk"):
        image_mode = "none"

    content = await file.read()
    res = convert_single_file(
        content, file.filename or "uploaded_file", image_mode=image_mode
    )
    if res["success"]:
        return JSONResponse(res)
    else:
        return JSONResponse(status_code=400, content=res)


@app.post("/api/convert-files")
async def convert_files(
    files: List[UploadFile] = File(...), image_mode: str = Form("local_disk")
):
    """Batch convert multiple files at once."""
    if md_converter is None:
        raise HTTPException(
            status_code=500, detail="MarkItDown converter is not initialized."
        )

    if not files:
        raise HTTPException(
            status_code=400, detail="Không có tệp tin nào được gửi lên."
        )

    results = []
    for idx, f in enumerate(files):
        content = await f.read()
        fname = f.filename or f"uploaded_file_{idx+1}"
        res = convert_single_file(content, fname, image_mode=image_mode)
        res["id"] = f"doc_{idx}"
        results.append(res)

    success_count = sum(1 for r in results if r.get("success"))
    return JSONResponse(
        {
            "success": True,
            "total_files": len(files),
            "success_count": success_count,
            "results": results,
        }
    )


@app.post("/api/convert-url")
async def convert_url(
    url: str = Form(...),
    image_mode: str = Form("local_disk"),
    embed_images: Optional[bool] = Form(None),
):
    if md_converter is None:
        raise HTTPException(
            status_code=500, detail="MarkItDown converter is not initialized."
        )

    if embed_images is False and image_mode in ("ref_base64", "local_disk"):
        image_mode = "none"

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="URL không hợp lệ. Vui lòng nhập link bắt đầu bằng http:// hoặc https://",
        )

    start_time = time.perf_counter()
    try:
        want_extract = image_mode != "none"
        result = md_converter.convert(url, keep_data_uris=want_extract)
        raw_markdown = result.text_content or ""
        doc_title = result.title or url

        markdown_text, image_info = process_extracted_images(
            raw_markdown, doc_name=doc_title, image_mode=image_mode
        )

        duration_ms = round((time.perf_counter() - start_time) * 1000, 1)
        stats = get_text_stats(markdown_text)

        single_res = {
            "success": True,
            "filename": url,
            "title": doc_title,
            "markdown": markdown_text,
            "file_size": len(markdown_text.encode("utf-8")),
            "duration_ms": duration_ms,
            "stats": stats,
            "image_info": image_info,
            "has_embedded_images": image_info["images_count"] > 0,
            "embedded_images_count": image_info["images_count"],
            "id": "doc_0",
        }
        return JSONResponse(
            {
                "success": True,
                "total_files": 1,
                "success_count": 1,
                "results": [single_res],
                **single_res,
            }
        )
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "filename": url,
                "error": str(e),
                "hint": "Không thể tải hoặc trích xuất nội dung từ URL này. Vui lòng kiểm tra lại đường dẫn.",
            },
        )


@app.post("/api/download-zip")
async def download_zip(
    folder_name: str = Form(...),
    filename: str = Form("document"),
    markdown_text: str = Form(...),
):
    """Packages a single document into a zip file with relative image paths (images/...)."""
    doc_img_dir = safe_resolve_image_folder(folder_name)
    base_name = sanitize_folder_name(filename)

    rel_markdown = markdown_text

    # Replace local disk paths
    rel_markdown = re.sub(
        rf'(?:file:///[a-zA-Z]:|[a-zA-Z]:)[/\\](?:[^/\\]+[/\\])*extracted_images[/\\]{re.escape(doc_img_dir.name)}[/\\]([^\)\s"\'>]+)',
        r"images/\1",
        rel_markdown,
        flags=re.IGNORECASE,
    )

    # Replace reference-style ![alt][img_X] with ![alt](images/image_X.png)
    def ref_to_rel(match):
        alt = match.group(1)
        idx = match.group(2)
        for ext in (".png", ".jpg", ".svg", ".webp", ".gif"):
            candidate = f"image_{idx}{ext}"
            if (doc_img_dir / candidate).exists():
                return f"![{alt}](images/{candidate})"
        return f"![{alt}](images/image_{idx}.png)"

    rel_markdown = re.sub(r"!\[([^\]]*)\]\[img_(\d+)\]", ref_to_rel, rel_markdown)

    # Remove the bottom assets section if present
    rel_markdown = re.sub(
        r"\n+<!-- =+ -->\n<!-- BỘ NHỚ HÌNH ẢNH \(IMAGE ASSETS\)[\s\S]*$",
        "",
        rel_markdown,
    )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr(f"{base_name}.md", rel_markdown)
        if doc_img_dir.exists():
            for img_file in doc_img_dir.iterdir():
                if img_file.is_file():
                    safe_arcname = f"images/{img_file.name}"
                    zip_file.write(img_file, arcname=safe_arcname)

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{base_name}_with_images.zip"'
        },
    )


@app.post("/api/download-all-zip")
async def download_all_zip(batch_json: str = Form(...)):
    """Packages all successfully converted Markdown files and images into a single zip file."""
    try:
        items = json.loads(batch_json)
        if not isinstance(items, list):
            raise ValueError("Expected JSON array")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Dữ liệu JSON không hợp lệ: {e}")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for idx, item in enumerate(items):
            raw_filename = item.get("filename", f"document_{idx+1}")
            base_name = sanitize_folder_name(raw_filename)
            md_content = item.get("markdown", "")
            raw_folder = item.get("folder_name", "")

            if raw_folder:
                clean_folder = sanitize_folder_name(raw_folder)
                doc_img_dir = safe_resolve_image_folder(clean_folder)
                rel_md = re.sub(
                    rf'(?:file:///[a-zA-Z]:|[a-zA-Z]:)[/\\](?:[^/\\]+[/\\])*extracted_images[/\\]{re.escape(clean_folder)}[/\\]([^\)\s"\'>]+)',
                    rf"images_{base_name}/\1",
                    md_content,
                    flags=re.IGNORECASE,
                )
                zip_file.writestr(f"{base_name}.md", rel_md)
                if doc_img_dir.exists():
                    for img_file in doc_img_dir.iterdir():
                        if img_file.is_file():
                            zip_file.write(
                                img_file,
                                arcname=f"images_{base_name}/{img_file.name}",
                            )
            else:
                zip_file.writestr(f"{base_name}.md", md_content)

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="all_converted_markdown.zip"'
        },
    )


@app.post("/api/open-folder")
async def open_folder(folder_path: str = Form(...)):
    """Safely opens an image folder under extracted_images in Windows Explorer."""
    clean_name = sanitize_folder_name(folder_path)
    target = safe_resolve_image_folder(clean_name)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Thư mục không tồn tại.")

    if sys.platform == "win32":
        try:
            os.startfile(str(target))
            return {"success": True, "message": f"Đã mở thư mục: {target.name}"}
        except Exception as e:
            return JSONResponse(
                status_code=500, content={"success": False, "error": str(e)}
            )
    return {"success": True, "message": f"Thư mục: {target.name}"}


# HTML UI Template with Design System & Responsive Layout
HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="vi" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MarkItDown Studio 2.0 — Dual Sync Scroll Document Inspector</title>

  <!-- Google Fonts: Plus Jakarta Sans & JetBrains Mono -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">

  <!-- Tailwind CSS -->
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['"Plus Jakarta Sans"', 'system-ui', '-apple-system', 'sans-serif'],
            mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
          },
          colors: {
            brand: {
              50: '#f0f9ff',
              100: '#e0f2fe',
              400: '#38bdf8',
              500: '#0284c7',
              600: '#0369a1',
              700: '#075985',
            },
            surface: {
              base: 'var(--surface-base)',
              card: 'var(--surface-card)',
              raised: 'var(--surface-raised)',
              border: 'var(--surface-border)',
              muted: 'var(--surface-muted)',
            }
          }
        }
      }
    }
  </script>

  <!-- Lucide Icons -->
  <script src="https://unpkg.com/lucide@latest"></script>

  <!-- Marked.js for Markdown Rendering -->
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>

  <!-- Highlight.js for Syntax Highlighting -->
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css" id="hljsTheme">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>

  <!-- DOMPurify for XSS Protection -->
  <script src="https://cdn.jsdelivr.net/npm/dompurify/dist/purify.min.js"></script>

  <style>
    :root {
      --surface-base: #0B0F17;
      --surface-card: #111827;
      --surface-raised: #1F2937;
      --surface-border: #374151;
      --surface-muted: #1E293B;
      --text-main: #F8FAFC;
      --text-muted: #94A3B8;
    }

    html:not(.dark) {
      --surface-base: #F8FAFC;
      --surface-card: #FFFFFF;
      --surface-raised: #F1F5F9;
      --surface-border: #CBD5E1;
      --surface-muted: #E2E8F0;
      --text-main: #0F172A;
      --text-muted: #64748B;
    }

    /* Custom Scrollbars */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: var(--surface-card); }
    ::-webkit-scrollbar-thumb { background: var(--surface-border); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #64748B; }

    /* Rendered Markdown Typography */
    .prose-dark { color: #E2E8F0; line-height: 1.7; font-size: 0.95rem; }
    html:not(.dark) .prose-dark { color: #1E293B; }

    .prose-dark h1, .prose-dark h2, .prose-dark h3, .prose-dark h4 {
      font-weight: 700; margin-top: 1.5em; margin-bottom: 0.6em; line-height: 1.3;
    }
    .prose-dark h1 { font-size: 1.85rem; border-bottom: 1px solid var(--surface-border); padding-bottom: 0.3em; }
    .prose-dark h2 { font-size: 1.45rem; border-bottom: 1px solid var(--surface-border); padding-bottom: 0.25em; }
    .prose-dark h3 { font-size: 1.2rem; }
    .prose-dark p { margin-bottom: 1em; }
    .prose-dark ul { list-style-type: disc; padding-left: 1.5em; margin-bottom: 1em; }
    .prose-dark ol { list-style-type: decimal; padding-left: 1.5em; margin-bottom: 1em; }
    .prose-dark li { margin-bottom: 0.3em; }
    .prose-dark blockquote {
      border-left: 4px solid #0284C7; padding-left: 1rem; color: #94A3B8; font-style: italic;
      margin: 1.2em 0; background: rgba(2, 132, 199, 0.06); padding-top: 0.5rem; padding-bottom: 0.5rem;
      border-radius: 0 4px 4px 0;
    }
    .prose-dark table {
      width: 100%; border-collapse: collapse; margin: 1.5em 0; font-size: 0.9rem;
      display: block; overflow-x: auto;
    }
    .prose-dark th, .prose-dark td { border: 1px solid var(--surface-border); padding: 0.65rem 0.9rem; text-align: left; }
    .prose-dark th { background-color: var(--surface-raised); font-weight: 600; }
    .prose-dark tr:nth-child(even) { background-color: rgba(148, 163, 184, 0.05); }
    .prose-dark tr:hover { background-color: rgba(2, 132, 199, 0.08); }
    .prose-dark code:not(pre code) {
      background-color: var(--surface-raised); color: #0284C7; padding: 0.2em 0.4em; border-radius: 4px;
      font-family: 'JetBrains Mono', monospace; font-size: 0.88em;
    }
    .prose-dark pre {
      background-color: #0F172A !important; border: 1px solid #334155; border-radius: 8px;
      padding: 1rem; overflow-x: auto; margin: 1.2em 0;
    }
    .prose-dark pre code {
      font-family: 'JetBrains Mono', monospace; font-size: 0.88rem; background: transparent !important; padding: 0;
    }
    .prose-dark hr { border: 0; border-top: 1px solid var(--surface-border); margin: 2em 0; }
    .prose-dark a { color: #0284C7; text-decoration: underline; text-underline-offset: 3px; }
    .prose-dark a:hover { color: #38BDF8; }
    .prose-dark img {
      max-width: 100%; height: auto; border-radius: 8px; margin: 1.5em 0;
      border: 1px solid var(--surface-border); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.3);
    }

    @keyframes pulse-glow {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.92); }
    }
    .animate-pulse-glow { animation: pulse-glow 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }

    @media (prefers-reduced-motion: reduce) {
      * { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
    }

    @media print {
      header, #uploadSection, #slideJumpBar, #paneRaw, .no-print { display: none !important; }
      body { background: white !important; color: black !important; }
      #panePreview { border: none !important; box-shadow: none !important; height: auto !important; }
      #previewScrollContainer { height: auto !important; overflow: visible !important; padding: 0 !important; }
    }
  </style>
</head>
<body class="bg-surface-base text-slate-100 font-sans min-h-screen flex flex-col antialiased selection:bg-sky-500/30 selection:text-sky-200">

  <!-- Global Drag and Drop Overlay -->
  <div id="globalDropOverlay" class="fixed inset-0 z-50 bg-sky-950/80 backdrop-blur-sm border-4 border-dashed border-sky-400 hidden flex-col items-center justify-center pointer-events-none transition-all duration-200">
    <div class="p-8 rounded-2xl bg-surface-card border border-sky-500/40 shadow-2xl flex flex-col items-center gap-3 text-center">
      <div class="w-16 h-16 rounded-full bg-sky-500/20 text-sky-400 flex items-center justify-center animate-bounce">
        <i data-lucide="upload-cloud" class="w-8 h-8"></i>
      </div>
      <h2 class="text-xl font-bold text-white">Thả tệp tin vào bất kỳ đâu</h2>
      <p class="text-sm text-sky-200">Hỗ trợ PDF, DOCX, PPTX, XLSX, HTML, TXT, CSV, âm thanh...</p>
    </div>
  </div>

  <!-- Top Navigation Bar -->
  <header class="border-b border-surface-border bg-surface-card/85 backdrop-blur sticky top-0 z-40 px-6 py-3.5 flex items-center justify-between transition-colors">
    <div class="flex items-center gap-3">
      <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-sky-500 to-indigo-600 flex items-center justify-center shadow-lg shadow-sky-500/25 text-white font-bold">
        <i data-lucide="split" class="w-5 h-5"></i>
      </div>
      <div>
        <div class="flex items-center gap-2">
          <h1 class="text-base font-bold tracking-tight text-white flex items-center gap-2">
            MarkItDown Studio
            <span class="px-2 py-0.5 text-[11px] font-mono font-medium rounded-full bg-sky-950/80 text-sky-400 border border-sky-800/60">v2.0 Dual Sync</span>
          </h1>
        </div>
        <p class="text-xs text-slate-400 hidden sm:block">Đối chiếu song song 1:1 giữa Mã nguồn Markdown và Bản xem trước trực quan</p>
      </div>
    </div>

    <!-- Quick Format Badges & Status -->
    <div class="hidden xl:flex items-center gap-1.5 text-xs text-slate-400">
      <span class="text-slate-500 text-[11px]">Định dạng:</span>
      <span class="px-1.5 py-0.5 rounded bg-surface-raised border border-surface-border text-rose-400 font-mono text-[10px]">PDF</span>
      <span class="px-1.5 py-0.5 rounded bg-surface-raised border border-surface-border text-sky-400 font-mono text-[10px]">DOCX</span>
      <span class="px-1.5 py-0.5 rounded bg-surface-raised border border-surface-border text-amber-400 font-mono text-[10px]">PPTX</span>
      <span class="px-1.5 py-0.5 rounded bg-surface-raised border border-surface-border text-emerald-400 font-mono text-[10px]">XLSX</span>
      <span class="px-1.5 py-0.5 rounded bg-surface-raised border border-surface-border text-indigo-400 font-mono text-[10px]">HTML</span>
      <span class="px-1.5 py-0.5 rounded bg-surface-raised border border-surface-border text-purple-400 font-mono text-[10px]">AUDIO</span>
    </div>

    <div class="flex items-center gap-2.5">
      <!-- Theme Switcher -->
      <button
        id="themeToggleBtn"
        onclick="toggleTheme()"
        class="w-8 h-8 rounded-lg bg-surface-raised border border-surface-border text-slate-300 hover:text-white flex items-center justify-center transition focus-visible:ring-2 focus-visible:ring-sky-500"
        title="Chuyển chế độ Sáng / Tối"
        aria-label="Toggle dark/light theme"
      >
        <i data-lucide="sun" class="w-4 h-4 hidden dark:block"></i>
        <i data-lucide="moon" class="w-4 h-4 block dark:hidden"></i>
      </button>

      <!-- Keyboard Shortcuts Modal Trigger -->
      <button
        onclick="toggleShortcutsModal()"
        class="w-8 h-8 rounded-lg bg-surface-raised border border-surface-border text-slate-300 hover:text-white flex items-center justify-center transition focus-visible:ring-2 focus-visible:ring-sky-500"
        title="Phím tắt tiện ích"
        aria-label="Keyboard shortcuts"
      >
        <i data-lucide="keyboard" class="w-4 h-4"></i>
      </button>

      <span id="systemStatus" class="flex items-center gap-1.5 text-xs text-emerald-400 font-medium bg-emerald-950/40 border border-emerald-800/40 px-2.5 py-1 rounded-full">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse-glow"></span>
        Sẵn sàng
      </span>
    </div>
  </header>

  <!-- Main Container -->
  <main class="flex-1 max-w-[1720px] w-full mx-auto p-4 sm:p-6 flex flex-col gap-5">

    <!-- Upload Section -->
    <section id="uploadSection" class="bg-surface-card border border-surface-border rounded-2xl p-5 sm:p-6 shadow-xl transition-colors">
      <div class="flex items-center justify-between border-b border-surface-border pb-3.5 mb-4">
        <div class="flex items-center gap-2">
          <button
            id="tabFileBtn"
            onclick="switchInputTab('file')"
            class="flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-sm font-semibold transition bg-sky-500/15 text-sky-400 border border-sky-500/30 focus-visible:ring-2 focus-visible:ring-sky-500"
          >
            <i data-lucide="files" class="w-4 h-4"></i>
            Tải lên tài liệu (Chọn nhiều file)
          </button>
          <button
            id="tabUrlBtn"
            onclick="switchInputTab('url')"
            class="flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-sm font-medium transition text-slate-400 hover:text-slate-200 hover:bg-surface-raised border border-transparent focus-visible:ring-2 focus-visible:ring-sky-500"
          >
            <i data-lucide="globe" class="w-4 h-4"></i>
            Trích xuất từ URL
          </button>
        </div>

        <div id="quickClearWrapper" class="hidden">
          <button
            onclick="resetAll()"
            class="flex items-center gap-1.5 text-xs text-slate-400 hover:text-rose-400 transition px-2.5 py-1.5 rounded-lg hover:bg-surface-raised focus-visible:ring-2 focus-visible:ring-rose-500"
          >
            <i data-lucide="rotate-ccw" class="w-3.5 h-3.5"></i>
            Làm mới / Nạp đợt khác
          </button>
        </div>
      </div>

      <!-- File Dropzone -->
      <div id="fileUploadContainer">
        <div
          id="dropzone"
          tabindex="0"
          role="button"
          aria-label="Upload files dropzone"
          ondragover="handleDragOver(event)"
          ondragleave="handleDragLeave(event)"
          ondrop="handleDrop(event)"
          onclick="document.getElementById('fileInput').click()"
          onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault(); document.getElementById('fileInput').click();}"
          class="border-2 border-dashed border-slate-700 hover:border-sky-500/70 bg-surface-base/60 hover:bg-sky-500/[0.03] rounded-2xl p-8 sm:p-11 transition duration-200 cursor-pointer flex flex-col items-center justify-center text-center group relative overflow-hidden focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:outline-none"
        >
          <input
            type="file"
            id="fileInput"
            multiple
            class="hidden"
            onchange="handleFileSelect(event)"
            accept=".pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.html,.htm,.txt,.csv,.json,.xml,.zip,.mp3,.wav,.m4a"
          >
          <div class="w-14 h-14 rounded-2xl bg-surface-raised group-hover:scale-105 border border-surface-border flex items-center justify-center text-sky-400 mb-3.5 transition shadow-inner">
            <i data-lucide="upload-cloud" class="w-7 h-7"></i>
          </div>
          <h2 class="text-base font-semibold text-slate-200 group-hover:text-sky-300 transition">
            Kéo thả <span class="text-sky-400 font-bold underline underline-offset-4 decoration-sky-400/50">một hoặc nhiều tệp tin</span> vào đây hoặc chọn từ thiết bị
          </h2>
          <p class="text-xs text-slate-400 mt-1.5 max-w-lg leading-relaxed">
            Hỗ trợ toàn diện PowerPoint (.pptx), Word (.docx), PDF, Excel (.xlsx), HTML, CSV, TXT, JSON... Chuyển đổi song song cực nhanh và đối chiếu 1:1.
          </p>
        </div>

        <!-- Image Extraction Mode Selector -->
        <div class="mt-4 p-4 rounded-xl bg-surface-raised/40 border border-surface-border">
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-3">
            <span class="text-xs font-bold text-slate-200 flex items-center gap-1.5">
              <i data-lucide="image" class="w-4 h-4 text-sky-400"></i>
              Phương thức xử lý hình ảnh trích xuất:
            </span>
            <span id="storagePathBadge" class="text-[11px] px-2 py-0.5 rounded bg-sky-950/70 text-sky-300 border border-sky-800/50 font-mono">
              Lưu trữ tại: extracted_images/
            </span>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
            <!-- Option 1: Lưu ổ đĩa sạch (0% Base64 - Khuyên dùng) -->
            <label id="cardLocalDisk" class="relative flex items-start gap-3 p-3.5 rounded-xl border border-emerald-500/50 bg-emerald-500/10 cursor-pointer transition group">
              <input type="radio" name="imageModeRadio" value="local_disk" checked onchange="handleImageModeSelect('local_disk')" class="mt-1 text-emerald-500 focus:ring-0 cursor-pointer">
              <div class="flex-1">
                <div class="flex items-center gap-1.5">
                  <span class="text-xs font-bold text-white group-hover:text-emerald-300 transition">Đường dẫn ổ đĩa (0% Base64)</span>
                  <span class="text-[10px] px-1.5 py-0.2 rounded bg-emerald-500/20 text-emerald-300 font-semibold border border-emerald-500/30">AI-Ready</span>
                </div>
                <p class="text-[11px] text-slate-300 mt-1 leading-snug">
                  Lưu ảnh vật lý vào thư mục riêng, trong file Markdown chỉ chứa đường dẫn ảnh gọn nhẹ. Không làm tràn token context khi nạp vào AI!
                </p>
              </div>
            </label>

            <!-- Option 2: Tham chiếu Base64 cuối file -->
            <label id="cardRefBase64" class="relative flex items-start gap-3 p-3.5 rounded-xl border border-surface-border bg-surface-card/60 cursor-pointer hover:bg-surface-raised transition group">
              <input type="radio" name="imageModeRadio" value="ref_base64" onchange="handleImageModeSelect('ref_base64')" class="mt-1 text-sky-500 focus:ring-0 cursor-pointer">
              <div class="flex-1">
                <span class="text-xs font-bold text-slate-200 group-hover:text-white transition">Tham chiếu Base64 cuối file</span>
                <p class="text-[11px] text-slate-400 mt-1 leading-snug">
                  Trong nội dung chỉ hiện <code class="text-sky-300 font-mono text-[10px]">![ảnh][img_1]</code>, dữ liệu Base64 gom xuống cuối file để xem độc lập không cần thư mục đính kèm.
                </p>
              </div>
            </label>

            <!-- Option 3: Bỏ qua ảnh -->
            <label id="cardNone" class="relative flex items-start gap-3 p-3.5 rounded-xl border border-surface-border bg-surface-card/60 cursor-pointer hover:bg-surface-raised transition group">
              <input type="radio" name="imageModeRadio" value="none" onchange="handleImageModeSelect('none')" class="mt-1 text-sky-500 focus:ring-0 cursor-pointer">
              <div class="flex-1">
                <span class="text-xs font-bold text-slate-200 group-hover:text-white transition">Chỉ văn bản thuần túy</span>
                <p class="text-[11px] text-slate-400 mt-1 leading-snug">
                  Tối giản tối đa, chỉ bóc tách cấu trúc văn bản, tiêu đề và bảng biểu, bỏ qua hoàn toàn hình ảnh.
                </p>
              </div>
            </label>
          </div>
        </div>
      </div>

      <!-- URL Input -->
      <div id="urlInputContainer" class="hidden">
        <form onsubmit="handleUrlSubmit(event)" class="flex flex-col sm:flex-row gap-3">
          <div class="relative flex-1">
            <div class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-slate-500">
              <i data-lucide="link-2" class="w-4 h-4"></i>
            </div>
            <input
              type="url"
              id="urlField"
              placeholder="Nhập liên kết trang web (ví dụ: https://en.wikipedia.org/wiki/Markdown)"
              required
              class="w-full pl-10 pr-4 py-2.5 rounded-xl bg-surface-base border border-surface-border focus:border-sky-500 focus:ring-2 focus:ring-sky-500/20 text-sm text-slate-100 placeholder-slate-500 outline-none transition"
            >
          </div>
          <button
            type="submit"
            class="px-5 py-2.5 rounded-xl bg-sky-600 hover:bg-sky-500 text-white font-semibold text-sm transition flex items-center justify-center gap-2 shadow-lg shadow-sky-600/20 active:scale-[0.98] focus-visible:ring-2 focus-visible:ring-sky-500"
          >
            <i data-lucide="sparkles" class="w-4 h-4"></i>
            Trích xuất Markdown
          </button>
        </form>
      </div>

      <!-- Loading Progress Indicator -->
      <div id="loadingIndicator" class="hidden mt-4 pt-4 border-t border-surface-border">
        <div class="flex items-center gap-3">
          <div class="w-5 h-5 border-2 border-sky-400 border-t-transparent rounded-full animate-spin"></div>
          <div class="flex-1">
            <p id="loadingStatusText" class="text-sm font-medium text-sky-400">Đang chuyển đổi tài liệu...</p>
            <div class="w-full bg-slate-800 rounded-full h-1.5 mt-2 overflow-hidden">
              <div id="loadingProgressBar" class="bg-sky-500 h-1.5 rounded-full transition-all duration-300 w-1/3 animate-pulse"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- Error Message Banner -->
      <div id="errorMessageBanner" class="hidden mt-4 p-4 rounded-xl bg-rose-950/40 border border-rose-800/60 text-rose-200">
        <div class="flex items-start gap-3">
          <i data-lucide="alert-circle" class="w-5 h-5 text-rose-400 shrink-0 mt-0.5"></i>
          <div class="text-xs space-y-1">
            <p class="font-bold text-sm text-rose-300" id="errorTitle">Không thể chuyển đổi tài liệu</p>
            <p id="errorDetail" class="font-mono text-slate-300 break-all"></p>
            <p id="errorHint" class="text-slate-400 italic"></p>
          </div>
        </div>
      </div>
    </section>

    <!-- Converted Documents Workspace -->
    <section id="resultsWorkspace" class="hidden flex-1 flex flex-col gap-4">

      <!-- Navigation Bar: File Switcher Bar -->
      <div class="bg-surface-card border border-surface-border rounded-2xl p-3 shadow-md flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3">
        <div class="flex items-center gap-2 overflow-x-auto py-1 scrollbar-thin flex-1 min-w-0" id="fileTabsContainer">
          <!-- Dynamic Document Tabs -->
        </div>

        <div class="flex items-center gap-2 shrink-0 border-t sm:border-t-0 sm:border-l border-surface-border pt-2 sm:pt-0 sm:pl-3">
          <button
            id="downloadAllZipBtn"
            onclick="downloadAllAsZip()"
            class="w-full sm:w-auto px-4 py-2 rounded-xl bg-gradient-to-r from-sky-600 to-indigo-600 hover:from-sky-500 hover:to-indigo-500 text-white font-bold text-xs transition flex items-center justify-center gap-2 shadow-lg shadow-sky-600/20 active:scale-95 focus-visible:ring-2 focus-visible:ring-sky-500"
            title="Tải về toàn bộ các file Markdown đã chuyển đổi trong 1 tệp ZIP"
          >
            <i data-lucide="archive" class="w-4 h-4"></i>
            <span>Tải tất cả (ZIP)</span>
            <span id="batchCountBadge" class="px-1.5 py-0.2 rounded-full bg-white/20 text-[10px] font-mono">0</span>
          </button>
        </div>
      </div>

      <!-- Image Extraction Summary Card -->
      <div id="imageFolderCard" class="hidden p-3.5 rounded-2xl bg-surface-card border border-sky-500/30 shadow-lg flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div class="flex items-center gap-3 min-w-0">
          <div class="w-10 h-10 rounded-xl bg-sky-500/15 text-sky-400 flex items-center justify-center shrink-0 border border-sky-500/30">
            <i data-lucide="folder-check" class="w-5 h-5"></i>
          </div>
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span class="text-xs font-bold text-white" id="imageSavedSummary">Đã bóc tách 0 ảnh</span>
              <span class="text-[10px] px-1.5 py-0.2 rounded bg-emerald-950 text-emerald-400 border border-emerald-800 font-mono">Đã lưu ổ đĩa</span>
            </div>
            <p class="text-xs text-sky-300 font-mono mt-0.5 truncate select-all" id="imageFolderPathText">
              extracted_images/
            </p>
          </div>
        </div>
        <div class="flex items-center gap-2 shrink-0 w-full sm:w-auto">
          <button onclick="copyImageFolderPath()" class="flex-1 sm:flex-none px-3 py-1.5 rounded-lg bg-surface-raised hover:bg-slate-700 border border-surface-border text-xs text-slate-200 font-medium transition flex items-center justify-center gap-1.5 focus-visible:ring-2 focus-visible:ring-sky-500">
            <i data-lucide="copy" class="w-3.5 h-3.5"></i>
            Sao chép đường dẫn
          </button>
          <button onclick="openImageFolderOnDisk()" class="flex-1 sm:flex-none px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-xs text-white font-bold transition flex items-center justify-center gap-1.5 shadow-md shadow-sky-600/20 focus-visible:ring-2 focus-visible:ring-sky-500">
            <i data-lucide="folder-open" class="w-3.5 h-3.5"></i>
            Mở thư mục ảnh
          </button>
        </div>
      </div>

      <!-- Action Toolbar & Metadata Bar -->
      <div class="bg-surface-card border border-surface-border rounded-2xl p-3.5 flex flex-wrap items-center justify-between gap-3 shadow-md">
        <div class="flex items-center gap-3 min-w-0">
          <div class="w-9 h-9 rounded-xl bg-sky-950/80 border border-sky-800/50 flex items-center justify-center text-sky-400 shrink-0">
            <i data-lucide="file-check" class="w-5 h-5"></i>
          </div>
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span id="metaFileName" class="text-sm font-bold text-white truncate max-w-[260px] sm:max-w-md">filename.ext</span>
              <span id="metaDuration" class="px-2 py-0.5 text-[11px] font-mono rounded bg-surface-raised border border-surface-border text-sky-300">0.0 ms</span>
              <span id="metaImageBadge" class="hidden px-2 py-0.5 text-[11px] font-mono font-bold rounded bg-emerald-950/80 text-emerald-400 border border-emerald-800/60 flex items-center gap-1">
                <i data-lucide="image" class="w-3 h-3"></i>
                <span id="metaImageCount">0 ảnh</span>
              </span>
            </div>
            <div class="flex items-center gap-2.5 text-xs text-slate-400 font-mono mt-0.5">
              <span id="metaWordCount">0 từ</span>
              <span>•</span>
              <span id="metaCharCount">0 ký tự</span>
              <span>•</span>
              <span id="metaTokenCount">~0 tokens</span>
              <span>•</span>
              <span id="metaReadingTime" class="text-slate-400 font-sans">~1 phút đọc</span>
            </div>
          </div>
        </div>

        <div class="flex items-center flex-wrap gap-2">
          <!-- View Modes -->
          <div class="bg-surface-base p-1 rounded-xl border border-surface-border flex items-center gap-1 text-xs">
            <button id="viewSplitBtn" onclick="setViewMode('split')" class="px-2.5 py-1 rounded-lg font-medium transition bg-sky-600 text-white shadow-sm flex items-center gap-1.5 focus-visible:ring-2 focus-visible:ring-sky-500">
              <i data-lucide="columns-2" class="w-3.5 h-3.5"></i>
              Song song
            </button>
            <button id="viewRawBtn" onclick="setViewMode('raw')" class="px-2.5 py-1 rounded-lg font-medium text-slate-400 hover:text-white transition flex items-center gap-1.5 focus-visible:ring-2 focus-visible:ring-sky-500">
              <i data-lucide="code" class="w-3.5 h-3.5"></i>
              Bản gốc (Raw)
            </button>
            <button id="viewPreviewBtn" onclick="setViewMode('preview')" class="px-2.5 py-1 rounded-lg font-medium text-slate-400 hover:text-white transition flex items-center gap-1.5 focus-visible:ring-2 focus-visible:ring-sky-500">
              <i data-lucide="eye" class="w-3.5 h-3.5"></i>
              Bản xem trước
            </button>
          </div>

          <!-- Synchronized Scroll Toggle -->
          <button
            id="syncScrollBtn"
            onclick="toggleSyncScroll()"
            class="px-2.5 py-1.5 rounded-xl font-medium text-xs transition bg-sky-500/15 text-sky-300 border border-sky-500/40 hover:bg-sky-500/25 flex items-center gap-1.5 shadow-sm focus-visible:ring-2 focus-visible:ring-sky-500"
            title="Bật/Tắt cuộn trang song song đồng bộ (Ctrl+Shift+S)"
          >
            <i data-lucide="link" class="w-3.5 h-3.5" id="syncScrollIcon"></i>
            <span id="syncScrollText">Cuộn song song: BẬT</span>
          </button>

          <div class="h-6 w-[1px] bg-surface-border mx-1 hidden sm:block"></div>

          <!-- Copy Button -->
          <button
            id="copyBtn"
            onclick="copyMarkdownToClipboard()"
            class="px-3 py-1.5 rounded-xl bg-surface-raised hover:bg-slate-700 border border-surface-border text-slate-200 hover:text-white font-medium text-xs transition flex items-center gap-1.5 shadow-sm active:scale-95 focus-visible:ring-2 focus-visible:ring-sky-500"
            title="Sao chép Markdown vào bộ nhớ tạm (Ctrl+Shift+C)"
          >
            <i data-lucide="copy" class="w-3.5 h-3.5" id="copyIcon"></i>
            <span id="copyBtnText">Sao chép</span>
          </button>

          <!-- Export HTML Button -->
          <button
            onclick="exportStandaloneHtml()"
            class="px-3 py-1.5 rounded-xl bg-indigo-700 hover:bg-indigo-600 text-white font-bold text-xs transition flex items-center gap-1.5 shadow-md shadow-indigo-700/20 active:scale-95 focus-visible:ring-2 focus-visible:ring-indigo-500"
            title="Xuất bản HTML hoàn chỉnh để xem offline hoặc chia sẻ"
          >
            <i data-lucide="globe" class="w-3.5 h-3.5"></i>
            Xuất HTML
          </button>

          <!-- Download ZIP Package for this specific file -->
          <button
            id="downloadZipBtn"
            onclick="downloadZipPackage()"
            class="hidden px-3.5 py-1.5 rounded-xl bg-sky-700 hover:bg-sky-600 text-white font-bold text-xs transition flex items-center gap-1.5 shadow-md shadow-sky-700/20 active:scale-95 focus-visible:ring-2 focus-visible:ring-sky-500"
            title="Tải về file .zip chứa file .md và thư mục images/ đính kèm"
          >
            <i data-lucide="package" class="w-3.5 h-3.5"></i>
            Tải ZIP (.md + ảnh)
          </button>

          <!-- Download .md Button -->
          <button
            onclick="downloadMarkdownFile()"
            class="px-3.5 py-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs transition flex items-center gap-1.5 shadow-md shadow-emerald-600/20 active:scale-95 focus-visible:ring-2 focus-visible:ring-emerald-500"
            title="Tải về tệp .md"
          >
            <i data-lucide="download" class="w-3.5 h-3.5"></i>
            Tải .md
          </button>
        </div>
      </div>

      <!-- Slide / Section Jump Navigation Bar -->
      <div id="slideJumpBar" class="hidden p-2.5 rounded-2xl bg-surface-card border border-surface-border flex items-center gap-2 overflow-x-auto scrollbar-thin shadow-inner">
        <span class="text-xs font-bold text-sky-400 flex items-center gap-1.5 shrink-0 pl-1">
          <i data-lucide="compass" class="w-4 h-4 text-sky-400"></i>
          Nhảy theo phần / Slide:
        </span>
        <div id="slidePillsContainer" class="flex items-center gap-1.5 overflow-x-auto py-0.5">
          <!-- Dynamic Pills -->
        </div>
      </div>

      <!-- Dual Viewer Grid with Equal Heights & Synchronized Scroll -->
      <div id="viewerGrid" class="grid grid-cols-1 lg:grid-cols-2 gap-4 flex-1">
        <!-- Left Pane: Markdown Raw Source -->
        <div id="paneRaw" class="bg-surface-card border border-surface-border rounded-2xl flex flex-col overflow-hidden shadow-lg transition-colors">
          <div class="px-4 py-2.5 border-b border-surface-border bg-surface-raised/40 flex items-center justify-between text-xs">
            <span class="font-mono text-slate-300 font-semibold flex items-center gap-1.5">
              <i data-lucide="file-code" class="w-4 h-4 text-sky-400"></i>
              Mã nguồn Markdown (Raw)
            </span>
            <span class="text-[11px] text-slate-400 font-mono" id="rawLineCount">0 dòng</span>
          </div>
          <div class="relative flex-1 flex">
            <textarea
              id="rawMarkdownTextarea"
              class="w-full h-[720px] p-5 font-mono text-xs sm:text-sm text-slate-200 bg-surface-card border-none outline-none resize-none leading-relaxed overflow-y-auto selection:bg-sky-500/30 scrollbar-thin focus-visible:ring-1 focus-visible:ring-sky-500"
              spellcheck="false"
              placeholder="Nội dung Markdown sẽ hiển thị tại đây..."
              oninput="handleRawContentEdit()"
            ></textarea>
          </div>
        </div>

        <!-- Right Pane: Rendered Visual Preview -->
        <div id="panePreview" class="bg-surface-card border border-surface-border rounded-2xl flex flex-col overflow-hidden shadow-lg transition-colors">
          <div class="px-4 py-2.5 border-b border-surface-border bg-surface-raised/40 flex items-center justify-between text-xs">
            <span class="font-medium text-slate-300 font-semibold flex items-center gap-1.5">
              <i data-lucide="sparkles" class="w-4 h-4 text-emerald-400"></i>
              Bản xem trước trực quan (Rendered Preview)
            </span>
            <span class="text-[11px] text-slate-400 flex items-center gap-1">
              <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
              Cuộn đồng bộ 1:1
            </span>
          </div>
          <div id="previewScrollContainer" class="h-[720px] p-6 overflow-y-auto bg-surface-card scrollbar-thin">
            <article id="renderedMarkdownContent" class="prose-dark max-w-none pb-28"></article>
          </div>
        </div>
      </div>
    </section>

  </main>

  <!-- Keyboard Shortcuts Modal -->
  <div id="shortcutsModal" class="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm hidden items-center justify-center p-4">
    <div class="bg-surface-card border border-surface-border rounded-2xl max-w-md w-full p-6 shadow-2xl space-y-4">
      <div class="flex items-center justify-between border-b border-surface-border pb-3">
        <h3 class="text-base font-bold text-white flex items-center gap-2">
          <i data-lucide="command" class="w-5 h-5 text-sky-400"></i>
          Phím tắt tiện ích
        </h3>
        <button onclick="toggleShortcutsModal()" class="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-surface-raised">
          <i data-lucide="x" class="w-4 h-4"></i>
        </button>
      </div>
      <div class="space-y-2 text-xs">
        <div class="flex items-center justify-between py-1.5 border-b border-surface-border/40">
          <span class="text-slate-300">Sao chép Markdown</span>
          <kbd class="px-2 py-0.5 rounded bg-surface-raised border border-surface-border font-mono text-[11px] text-sky-300">Ctrl + Shift + C</kbd>
        </div>
        <div class="flex items-center justify-between py-1.5 border-b border-surface-border/40">
          <span class="text-slate-300">Bật/Tắt cuộn song song</span>
          <kbd class="px-2 py-0.5 rounded bg-surface-raised border border-surface-border font-mono text-[11px] text-sky-300">Ctrl + Shift + S</kbd>
        </div>
        <div class="flex items-center justify-between py-1.5 border-b border-surface-border/40">
          <span class="text-slate-300">Chế độ Song song / Raw / Xem trước</span>
          <kbd class="px-2 py-0.5 rounded bg-surface-raised border border-surface-border font-mono text-[11px] text-sky-300">Alt + 1 / 2 / 3</kbd>
        </div>
        <div class="flex items-center justify-between py-1.5 border-b border-surface-border/40">
          <span class="text-slate-300">Trích xuất URL (khi nhập link)</span>
          <kbd class="px-2 py-0.5 rounded bg-surface-raised border border-surface-border font-mono text-[11px] text-sky-300">Ctrl + Enter</kbd>
        </div>
        <div class="flex items-center justify-between py-1.5">
          <span class="text-slate-300">In / Lưu PDF</span>
          <kbd class="px-2 py-0.5 rounded bg-surface-raised border border-surface-border font-mono text-[11px] text-sky-300">Ctrl + P</kbd>
        </div>
      </div>
    </div>
  </div>

  <!-- Notification Toast -->
  <div id="toast" class="fixed bottom-6 right-6 z-50 transform translate-y-16 opacity-0 transition-all duration-300 pointer-events-none flex items-center gap-2 px-4 py-2.5 rounded-xl bg-emerald-600 text-white font-medium text-xs shadow-2xl">
    <i data-lucide="check-circle" class="w-4 h-4"></i>
    <span id="toastMessage">Thao tác thành công!</span>
  </div>

  <script>
    // State
    let convertedDocs = [];
    let activeDocIndex = 0;
    let selectedImageMode = "local_disk";
    let activeInputTab = "file";
    let activeViewMode = "split";

    // Synchronized Scroll State
    let syncScrollEnabled = true;
    let isSyncing = false;
    let activeScroller = null;

    lucide.createIcons();
    initTheme();
    initGlobalDragAndDrop();
    initKeyboardShortcuts();

    function showToast(message) {
      const toast = document.getElementById('toast');
      const toastMsg = document.getElementById('toastMessage');
      toastMsg.innerText = message;
      toast.classList.remove('translate-y-16', 'opacity-0');
      toast.classList.add('translate-y-0', 'opacity-100');
      setTimeout(() => {
        toast.classList.remove('translate-y-0', 'opacity-100');
        toast.classList.add('translate-y-16', 'opacity-0');
      }, 2500);
    }

    function initTheme() {
      const savedTheme = localStorage.getItem('theme') || 'dark';
      if (savedTheme === 'light') {
        document.documentElement.classList.remove('dark');
      } else {
        document.documentElement.classList.add('dark');
      }
    }

    function toggleTheme() {
      const isDark = document.documentElement.classList.toggle('dark');
      localStorage.setItem('theme', isDark ? 'dark' : 'light');
      lucide.createIcons();
      showToast(isDark ? "Đã chuyển sang chế độ Tối (OLED)" : "Đã chuyển sang chế độ Sáng");
    }

    function toggleShortcutsModal() {
      const modal = document.getElementById('shortcutsModal');
      modal.classList.toggle('hidden');
      modal.classList.toggle('flex');
    }

    function toggleSyncScroll() {
      syncScrollEnabled = !syncScrollEnabled;
      const btn = document.getElementById('syncScrollBtn');
      const text = document.getElementById('syncScrollText');
      const icon = document.getElementById('syncScrollIcon');

      if (syncScrollEnabled) {
        btn.className = "px-2.5 py-1.5 rounded-xl font-medium text-xs transition bg-sky-500/15 text-sky-300 border border-sky-500/40 hover:bg-sky-500/25 flex items-center gap-1.5 shadow-sm focus-visible:ring-2 focus-visible:ring-sky-500";
        text.innerText = "Cuộn song song: BẬT";
        icon.setAttribute('data-lucide', 'link');
        showToast("Đã BẬT cuộn song song đồng bộ!");
      } else {
        btn.className = "px-2.5 py-1.5 rounded-xl font-medium text-xs transition bg-surface-raised text-slate-400 border border-surface-border hover:text-white flex items-center gap-1.5 shadow-sm focus-visible:ring-2 focus-visible:ring-sky-500";
        text.innerText = "Cuộn song song: TẮT";
        icon.setAttribute('data-lucide', 'unlink');
        showToast("Đã TẮT cuộn song song!");
      }
      lucide.createIcons();
    }

    function initScrollSync() {
      const raw = document.getElementById('rawMarkdownTextarea');
      const preview = document.getElementById('previewScrollContainer');
      if (!raw || !preview) return;

      raw.addEventListener('mouseenter', () => { activeScroller = 'raw'; });
      preview.addEventListener('mouseenter', () => { activeScroller = 'preview'; });
      raw.addEventListener('touchstart', () => { activeScroller = 'raw'; }, { passive: true });
      preview.addEventListener('touchstart', () => { activeScroller = 'preview'; }, { passive: true });

      // Raw -> Preview
      raw.addEventListener('scroll', () => {
        if (!syncScrollEnabled || isSyncing || activeScroller !== 'raw') return;
        isSyncing = true;
        const rawMax = raw.scrollHeight - raw.clientHeight;
        const prevMax = preview.scrollHeight - preview.clientHeight;
        if (rawMax > 0 && prevMax > 0) {
          const ratio = raw.scrollTop / rawMax;
          preview.scrollTop = ratio * prevMax;
        }
        requestAnimationFrame(() => { isSyncing = false; });
      });

      // Preview -> Raw
      preview.addEventListener('scroll', () => {
        if (!syncScrollEnabled || isSyncing || activeScroller !== 'preview') return;
        isSyncing = true;
        const rawMax = raw.scrollHeight - raw.clientHeight;
        const prevMax = preview.scrollHeight - preview.clientHeight;
        if (rawMax > 0 && prevMax > 0) {
          const ratio = preview.scrollTop / prevMax;
          raw.scrollTop = ratio * rawMax;
        }
        requestAnimationFrame(() => { isSyncing = false; });
      });
    }

    function jumpToSlide(slideNum) {
      const raw = document.getElementById('rawMarkdownTextarea');
      const preview = document.getElementById('previewScrollContainer');
      if (!raw || !preview) return;

      const targetEl = document.getElementById('slide-marker-' + slideNum);
      if (targetEl) {
        preview.scrollTo({
          top: targetEl.offsetTop - 15,
          behavior: 'smooth'
        });
      }

      const text = raw.value;
      const searchPattern = new RegExp(`<!--\\s*Slide number:\\s*${slideNum}\\s*-->`, 'i');
      const match = searchPattern.exec(text);
      if (match) {
        const charIndex = match.index;
        const linesBefore = text.substring(0, charIndex).split('\n').length;
        const totalLines = text.split('\n').length;
        const lineRatio = (linesBefore - 1) / Math.max(1, totalLines);
        const targetScrollTop = lineRatio * (raw.scrollHeight - raw.clientHeight);
        raw.scrollTo({
          top: targetScrollTop,
          behavior: 'smooth'
        });
      }

      document.querySelectorAll('.slide-pill-btn').forEach(btn => {
        const isCurrent = btn.getAttribute('data-slide') == slideNum;
        btn.className = isCurrent
          ? "slide-pill-btn px-2.5 py-1 rounded-lg text-xs font-mono font-bold bg-sky-600 text-white shadow-sm border border-sky-400 shrink-0"
          : "slide-pill-btn px-2.5 py-1 rounded-lg text-xs font-mono font-medium bg-surface-raised hover:bg-slate-700 text-slate-300 hover:text-white border border-surface-border shrink-0 transition";
      });

      showToast(`Đã đối chiếu tới Slide ${slideNum}!`);
    }

    function jumpToHeading(anchorId) {
      const preview = document.getElementById('previewScrollContainer');
      const targetEl = document.getElementById(anchorId);
      if (targetEl && preview) {
        preview.scrollTo({
          top: targetEl.offsetTop - 15,
          behavior: 'smooth'
        });
      }
    }

    function handleImageModeSelect(mode) {
      selectedImageMode = mode;
      const cardRef = document.getElementById('cardRefBase64');
      const cardDisk = document.getElementById('cardLocalDisk');
      const cardNone = document.getElementById('cardNone');

      [cardRef, cardDisk, cardNone].forEach(c => {
        c.className = "relative flex items-start gap-3 p-3.5 rounded-xl border border-surface-border bg-surface-card/60 cursor-pointer hover:bg-surface-raised transition group";
      });

      if (mode === 'local_disk') {
        cardDisk.className = "relative flex items-start gap-3 p-3.5 rounded-xl border border-emerald-500/50 bg-emerald-500/10 cursor-pointer transition group";
      } else if (mode === 'ref_base64') {
        cardRef.className = "relative flex items-start gap-3 p-3.5 rounded-xl border border-sky-500/50 bg-sky-500/10 cursor-pointer transition group";
      } else if (mode === 'none') {
        cardNone.className = "relative flex items-start gap-3 p-3.5 rounded-xl border border-amber-500/50 bg-amber-500/10 cursor-pointer transition group";
      }
    }

    function switchInputTab(tab) {
      activeInputTab = tab;
      const fileBtn = document.getElementById('tabFileBtn');
      const urlBtn = document.getElementById('tabUrlBtn');
      const fileContainer = document.getElementById('fileUploadContainer');
      const urlContainer = document.getElementById('urlInputContainer');

      if (tab === 'file') {
        fileBtn.className = "flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-sm font-semibold transition bg-sky-500/15 text-sky-400 border border-sky-500/30 focus-visible:ring-2 focus-visible:ring-sky-500";
        urlBtn.className = "flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-sm font-medium transition text-slate-400 hover:text-slate-200 hover:bg-surface-raised border border-transparent focus-visible:ring-2 focus-visible:ring-sky-500";
        fileContainer.classList.remove('hidden');
        urlContainer.classList.add('hidden');
      } else {
        urlBtn.className = "flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-sm font-semibold transition bg-sky-500/15 text-sky-400 border border-sky-500/30 focus-visible:ring-2 focus-visible:ring-sky-500";
        fileBtn.className = "flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-sm font-medium transition text-slate-400 hover:text-slate-200 hover:bg-surface-raised border border-transparent focus-visible:ring-2 focus-visible:ring-sky-500";
        urlContainer.classList.remove('hidden');
        fileContainer.classList.add('hidden');
      }
    }

    function initGlobalDragAndDrop() {
      const overlay = document.getElementById('globalDropOverlay');
      let dragCounter = 0;

      window.addEventListener('dragenter', (e) => {
        e.preventDefault();
        dragCounter++;
        overlay.classList.remove('hidden');
        overlay.classList.add('flex');
      });

      window.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dragCounter--;
        if (dragCounter <= 0) {
          dragCounter = 0;
          overlay.classList.add('hidden');
          overlay.classList.remove('flex');
        }
      });

      window.addEventListener('dragover', (e) => {
        e.preventDefault();
      });

      window.addEventListener('drop', (e) => {
        e.preventDefault();
        dragCounter = 0;
        overlay.classList.add('hidden');
        overlay.classList.remove('flex');
        const files = e.dataTransfer.files;
        if (files && files.length > 0) {
          uploadMultipleFiles(files);
        }
      });
    }

    function initKeyboardShortcuts() {
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          const modal = document.getElementById('shortcutsModal');
          if (!modal.classList.contains('hidden')) {
            toggleShortcutsModal();
          }
        }
        if (e.ctrlKey && e.shiftKey && (e.key === 'C' || e.key === 'c')) {
          e.preventDefault();
          copyMarkdownToClipboard();
        }
        if (e.ctrlKey && e.shiftKey && (e.key === 'S' || e.key === 's')) {
          e.preventDefault();
          toggleSyncScroll();
        }
        if (e.altKey && e.key === '1') {
          e.preventDefault();
          setViewMode('split');
        }
        if (e.altKey && e.key === '2') {
          e.preventDefault();
          setViewMode('raw');
        }
        if (e.altKey && e.key === '3') {
          e.preventDefault();
          setViewMode('preview');
        }
      });
    }

    function handleDragOver(e) {
      e.preventDefault(); e.stopPropagation();
      document.getElementById('dropzone').classList.add('border-sky-400', 'bg-sky-500/10');
    }

    function handleDragLeave(e) {
      e.preventDefault(); e.stopPropagation();
      document.getElementById('dropzone').classList.remove('border-sky-400', 'bg-sky-500/10');
    }

    function handleDrop(e) {
      e.preventDefault(); e.stopPropagation();
      document.getElementById('dropzone').classList.remove('border-sky-400', 'bg-sky-500/10');
      const files = e.dataTransfer.files;
      if (files && files.length > 0) uploadMultipleFiles(files);
    }

    function handleFileSelect(e) {
      const files = e.target.files;
      if (files && files.length > 0) uploadMultipleFiles(files);
    }

    function showLoading(msg) {
      const indicator = document.getElementById('loadingIndicator');
      document.getElementById('loadingStatusText').innerText = msg || "Đang chuyển đổi tài liệu...";
      indicator.classList.remove('hidden');
      document.getElementById('errorMessageBanner').classList.add('hidden');
    }

    function hideLoading() {
      document.getElementById('loadingIndicator').classList.add('hidden');
    }

    function showError(title, detail, hint) {
      const banner = document.getElementById('errorMessageBanner');
      document.getElementById('errorTitle').innerText = title;
      document.getElementById('errorDetail').innerText = detail;
      document.getElementById('errorHint').innerText = hint || "";
      banner.classList.remove('hidden');
      hideLoading();
    }

    async function uploadMultipleFiles(fileList) {
      const count = fileList.length;
      showLoading(`Đang nạp và chuyển đổi ${count} tệp tin cùng lúc...`);

      const formData = new FormData();
      for (let i = 0; i < count; i++) {
        formData.append('files', fileList[i]);
      }
      formData.append('image_mode', selectedImageMode);

      try {
        const response = await fetch('/api/convert-files', { method: 'POST', body: formData });
        const data = await response.json();
        hideLoading();

        if (response.ok && data.success && data.results && data.results.length > 0) {
          convertedDocs = data.results;
          activeDocIndex = 0;
          renderBatchWorkspace();
          showToast(`Đã chuyển đổi thành công ${data.success_count}/${data.total_files} tài liệu!`);
        } else {
          showError("Lỗi chuyển đổi tệp", data.error || "Không thể phân tích tệp tin", data.hint);
        }
      } catch (err) {
        hideLoading();
        showError("Lỗi kết nối", err.message, "Đảm bảo dịch vụ MarkItDown đang chạy tại máy cục bộ.");
      }
    }

    async function handleUrlSubmit(e) {
      e.preventDefault();
      const url = document.getElementById('urlField').value.trim();
      if (!url) return;

      showLoading(`Đang kết nối tới URL "${url}"...`);
      const formData = new FormData();
      formData.append('url', url);
      formData.append('image_mode', selectedImageMode);

      try {
        const response = await fetch('/api/convert-url', { method: 'POST', body: formData });
        const data = await response.json();
        hideLoading();

        if (response.ok && data.success && data.results && data.results.length > 0) {
          convertedDocs = data.results;
          activeDocIndex = 0;
          renderBatchWorkspace();
          showToast("Đã trích xuất Markdown từ URL thành công!");
        } else {
          showError("Lỗi trích xuất URL", data.error || "Không thể phân tích trang web", data.hint);
        }
      } catch (err) {
        hideLoading();
        showError("Lỗi kết nối mạng", err.message, "Kiểm tra lại kết nối internet hoặc đường dẫn URL.");
      }
    }

    function renderBatchWorkspace() {
      const tabsContainer = document.getElementById('fileTabsContainer');
      tabsContainer.innerHTML = "";

      document.getElementById('batchCountBadge').innerText = convertedDocs.length;

      convertedDocs.forEach((doc, idx) => {
        const tabBtn = document.createElement('button');
        tabBtn.onclick = () => switchActiveDoc(idx);

        const isActive = (idx === activeDocIndex);
        const imgCount = (doc.image_info && doc.image_info.images_count) ? doc.image_info.images_count : 0;
        const ext = doc.filename.split('.').pop().toUpperCase();

        if (isActive) {
          tabBtn.className = "flex items-center gap-2 px-3.5 py-2 rounded-xl text-xs font-bold bg-gradient-to-r from-sky-600 to-indigo-600 text-white shadow-md shadow-sky-600/30 shrink-0 transition border border-sky-400/50";
        } else {
          tabBtn.className = "flex items-center gap-2 px-3.5 py-2 rounded-xl text-xs font-medium bg-surface-raised hover:bg-slate-700 text-slate-300 hover:text-white shrink-0 transition border border-surface-border";
        }

        tabBtn.title = doc.filename;

        let badgeHtml = "";
        if (imgCount > 0) {
          badgeHtml = `<span class="px-1.5 py-0.2 rounded-full ${isActive ? 'bg-white/20 text-white' : 'bg-slate-800 text-sky-300'} text-[10px] font-mono">${imgCount} ảnh</span>`;
        } else {
          badgeHtml = `<span class="px-1.5 py-0.2 rounded-full ${isActive ? 'bg-white/20 text-white' : 'bg-slate-800 text-slate-400'} text-[10px] font-mono">${ext}</span>`;
        }

        tabBtn.innerHTML = `
          <i data-lucide="${doc.success ? 'file-text' : 'alert-triangle'}" class="w-3.5 h-3.5 ${doc.success ? '' : 'text-rose-400'}"></i>
          <span class="max-w-[150px] sm:max-w-[190px] truncate">${doc.filename}</span>
          ${badgeHtml}
        `;

        tabsContainer.appendChild(tabBtn);
      });

      renderActiveDocument();

      document.getElementById('resultsWorkspace').classList.remove('hidden');
      document.getElementById('quickClearWrapper').classList.remove('hidden');
      document.getElementById('resultsWorkspace').scrollIntoView({ behavior: 'smooth' });
      lucide.createIcons();
      initScrollSync();
    }

    function switchActiveDoc(idx) {
      if (idx < 0 || idx >= convertedDocs.length) return;
      activeDocIndex = idx;
      renderBatchWorkspace();
    }

    function renderActiveDocument() {
      const doc = convertedDocs[activeDocIndex];
      if (!doc) return;

      const currentMarkdown = doc.markdown || "";
      const currentFileName = doc.filename || "document.md";

      document.getElementById('metaFileName').innerText = currentFileName;
      document.getElementById('metaDuration').innerText = `${doc.duration_ms} ms`;
      document.getElementById('metaWordCount').innerText = `${doc.stats.words.toLocaleString()} từ`;
      document.getElementById('metaCharCount').innerText = `${doc.stats.characters.toLocaleString()} ký tự`;
      document.getElementById('metaTokenCount').innerText = `~${doc.stats.estimated_tokens.toLocaleString()} tokens`;
      document.getElementById('metaReadingTime').innerText = `~${doc.stats.reading_time_min || 1} phút đọc`;
      document.getElementById('rawLineCount').innerText = `${doc.stats.lines.toLocaleString()} dòng`;

      const imgBadge = document.getElementById('metaImageBadge');
      const imgFolderCard = document.getElementById('imageFolderCard');
      const zipBtn = document.getElementById('downloadZipBtn');

      if (doc.image_info && doc.image_info.images_count > 0) {
        imgBadge.classList.remove('hidden');
        const modeLabel = doc.image_info.image_mode === 'local_disk' ? '0% Base64' : (doc.image_info.image_mode === 'ref_base64' ? 'Tham chiếu Base64' : 'Không ảnh');
        document.getElementById('metaImageCount').innerText = `${doc.image_info.images_count} ảnh (${modeLabel})`;
        document.getElementById('imageSavedSummary').innerText = `Đã tách & lưu ${doc.image_info.images_count} ảnh:`;
        document.getElementById('imageFolderPathText').innerText = doc.image_info.folder_path || "";
        imgFolderCard.classList.remove('hidden');
        zipBtn.classList.remove('hidden');
      } else {
        imgBadge.classList.add('hidden');
        imgFolderCard.classList.add('hidden');
        zipBtn.classList.add('hidden');
      }

      // Populate Slide & Heading Navigation Bar
      const slideJumpBar = document.getElementById('slideJumpBar');
      const slidePillsContainer = document.getElementById('slidePillsContainer');
      const slideMatches = [...currentMarkdown.matchAll(/<!--\s*Slide number:\s*(\d+)\s*-->/gi)];
      const headingMatches = [...currentMarkdown.matchAll(/^(#{1,3})\s+(.+)$/gm)];

      if (slideMatches.length > 0 || headingMatches.length > 0) {
        slidePillsContainer.innerHTML = "";

        if (slideMatches.length > 0) {
          slideMatches.forEach((match, i) => {
            const num = match[1];
            const pill = document.createElement('button');
            pill.setAttribute('data-slide', num);
            pill.className = (i === 0)
              ? "slide-pill-btn px-2.5 py-1 rounded-lg text-xs font-mono font-bold bg-sky-600 text-white shadow-sm border border-sky-400 shrink-0"
              : "slide-pill-btn px-2.5 py-1 rounded-lg text-xs font-mono font-medium bg-surface-raised hover:bg-slate-700 text-slate-300 hover:text-white border border-surface-border shrink-0 transition";
            pill.innerText = `Slide ${num}`;
            pill.onclick = () => jumpToSlide(num);
            slidePillsContainer.appendChild(pill);
          });
        } else if (headingMatches.length > 0) {
          headingMatches.slice(0, 10).forEach((match, i) => {
            const level = match[1].length;
            const text = match[2].trim();
            const anchorId = `heading-${i}`;
            const pill = document.createElement('button');
            pill.className = "px-2.5 py-1 rounded-lg text-xs font-mono font-medium bg-surface-raised hover:bg-slate-700 text-slate-300 hover:text-white border border-surface-border shrink-0 transition max-w-[140px] truncate";
            pill.innerText = text;
            pill.title = text;
            pill.onclick = () => jumpToHeading(anchorId);
            slidePillsContainer.appendChild(pill);
          });
        }
        slideJumpBar.classList.remove('hidden');
      } else {
        slideJumpBar.classList.add('hidden');
      }

      const rawTextarea = document.getElementById('rawMarkdownTextarea');
      rawTextarea.value = currentMarkdown;

      updateRenderedPreview(currentMarkdown);
    }

    function updateRenderedPreview(markdownText) {
      const renderedContainer = document.getElementById('renderedMarkdownContent');
      if (!markdownText || markdownText.trim() === "") {
        renderedContainer.innerHTML = "<p class='text-slate-500 italic'>Không có nội dung để hiển thị.</p>";
        return;
      }

      marked.setOptions({
        gfm: true,
        breaks: true,
        highlight: function(code, lang) {
          if (lang && hljs.getLanguage(lang)) {
            try { return hljs.highlight(code, { language: lang }).value; } catch (__) {}
          }
          return hljs.highlightAuto(code).value;
        }
      });

      // Map local disk paths or relative extracted_images to web static path /extracted_images/... for preview
      let previewMarkdown = markdownText.replace(/!\[([^\]]*)\]\((?:file:\/\/\/[a-zA-Z]:|[a-zA-Z]:)?[^\r\n()"'<>]*?extracted[ _]images[/\\]([^\r\n()"'<>]+)\)/gi, function(match, alt, p1) {
        const cleanPath = p1.replace(/\\/g, '/').split('/').map(encodeURIComponent).join('/');
        return '![' + alt + '](/extracted_images/' + cleanPath + ')';
      });

      // Transform slide comment markers into visual anchor dividers
      previewMarkdown = previewMarkdown.replace(/<!--\s*Slide number:\s*(\d+)\s*-->/gi, function(match, num) {
        return `\n\n<div class="slide-anchor-divider my-8 pt-4 border-t-2 border-dashed border-sky-500/40 relative" id="slide-marker-${num}" data-slide="${num}">
          <div class="inline-flex items-center gap-2 px-3 py-1 rounded-lg bg-sky-500/15 border border-sky-500/30 text-sky-300 font-mono text-xs font-bold shadow-sm">
            <span class="w-2 h-2 rounded-full bg-sky-400 animate-pulse"></span>
            <span>TRANG CHIẾU / SLIDE ${num}</span>
          </div>
        </div>\n\n`;
      });

      // Add IDs to headings for jump navigation
      let headingIdx = 0;
      previewMarkdown = previewMarkdown.replace(/^(#{1,3})\s+(.+)$/gm, function(match, hashes, title) {
        const id = `heading-${headingIdx++}`;
        return `\n<a id="${id}"></a>\n${hashes} ${title}\n`;
      });

      const rawHtml = marked.parse(previewMarkdown);
      const cleanHtml = DOMPurify.sanitize(rawHtml, {
        ADD_TAGS: ['div', 'span', 'a'],
        ADD_ATTR: ['id', 'class', 'data-slide', 'target']
      });
      renderedContainer.innerHTML = cleanHtml;
    }

    function handleRawContentEdit() {
      const doc = convertedDocs[activeDocIndex];
      if (!doc) return;

      const edited = document.getElementById('rawMarkdownTextarea').value;
      doc.markdown = edited;
      updateRenderedPreview(edited);

      const lines = edited.split('\n').length;
      const words = edited.trim() ? edited.trim().split(/\s+/).length : 0;
      document.getElementById('rawLineCount').innerText = `${lines} dòng`;
      document.getElementById('metaWordCount').innerText = `${words} từ`;
    }

    function setViewMode(mode) {
      activeViewMode = mode;
      const grid = document.getElementById('viewerGrid');
      const paneRaw = document.getElementById('paneRaw');
      const panePreview = document.getElementById('panePreview');

      const btnSplit = document.getElementById('viewSplitBtn');
      const btnRaw = document.getElementById('viewRawBtn');
      const btnPreview = document.getElementById('viewPreviewBtn');

      [btnSplit, btnRaw, btnPreview].forEach(btn => {
        btn.className = "px-2.5 py-1 rounded-lg font-medium text-slate-400 hover:text-white transition flex items-center gap-1.5 focus-visible:ring-2 focus-visible:ring-sky-500";
      });

      if (mode === 'split') {
        btnSplit.className = "px-2.5 py-1 rounded-lg font-medium transition bg-sky-600 text-white shadow-sm flex items-center gap-1.5 focus-visible:ring-2 focus-visible:ring-sky-500";
        grid.className = "grid grid-cols-1 lg:grid-cols-2 gap-4 flex-1";
        paneRaw.classList.remove('hidden');
        panePreview.classList.remove('hidden');
      } else if (mode === 'raw') {
        btnRaw.className = "px-2.5 py-1 rounded-lg font-medium transition bg-sky-600 text-white shadow-sm flex items-center gap-1.5 focus-visible:ring-2 focus-visible:ring-sky-500";
        grid.className = "grid grid-cols-1 gap-4 flex-1";
        paneRaw.classList.remove('hidden');
        panePreview.classList.add('hidden');
      } else if (mode === 'preview') {
        btnPreview.className = "px-2.5 py-1 rounded-lg font-medium transition bg-sky-600 text-white shadow-sm flex items-center gap-1.5 focus-visible:ring-2 focus-visible:ring-sky-500";
        grid.className = "grid grid-cols-1 gap-4 flex-1";
        paneRaw.classList.add('hidden');
        panePreview.classList.remove('hidden');
      }
    }

    function copyMarkdownToClipboard() {
      const doc = convertedDocs[activeDocIndex];
      if (!doc || !doc.markdown) return;
      navigator.clipboard.writeText(doc.markdown).then(() => {
        const copyIcon = document.getElementById('copyIcon');
        const copyText = document.getElementById('copyBtnText');
        copyText.innerText = "Đã chép!";
        copyIcon.setAttribute('data-lucide', 'check');
        lucide.createIcons();
        showToast(`Đã sao chép Markdown của "${doc.filename}"!`);
        setTimeout(() => {
          copyText.innerText = "Sao chép";
          copyIcon.setAttribute('data-lucide', 'copy');
          lucide.createIcons();
        }, 2000);
      }).catch(err => {
        alert("Không thể sao chép: " + err);
      });
    }

    function copyImageFolderPath() {
      const doc = convertedDocs[activeDocIndex];
      if (!doc || !doc.image_info || !doc.image_info.folder_path) return;
      navigator.clipboard.writeText(doc.image_info.folder_path).then(() => {
        showToast("Đã sao chép đường dẫn thư mục ảnh!");
      });
    }

    async function openImageFolderOnDisk() {
      const doc = convertedDocs[activeDocIndex];
      if (!doc || !doc.image_info || !doc.image_info.folder_name) return;
      const formData = new FormData();
      formData.append('folder_path', doc.image_info.folder_name);
      try {
        const res = await fetch('/api/open-folder', { method: 'POST', body: formData });
        const data = await res.json();
        if (res.ok && data.success) {
          showToast("Đã mở thư mục ảnh trên thiết bị!");
        } else {
          showToast("Thông báo: " + (data.error || data.detail || data.message));
        }
      } catch (err) {
        showToast("Lỗi khi mở thư mục: " + err.message);
      }
    }

    async function downloadZipPackage() {
      const doc = convertedDocs[activeDocIndex];
      if (!doc || !doc.markdown) return;
      const folderName = doc.image_info ? doc.image_info.folder_name : "";
      if (!folderName) return;

      showToast(`Đang đóng gói file "${doc.filename}" và ảnh đính kèm...`);
      const formData = new FormData();
      formData.append('folder_name', folderName);
      formData.append('filename', doc.filename);
      formData.append('markdown_text', doc.markdown);

      try {
        const resp = await fetch('/api/download-zip', { method: 'POST', body: formData });
        if (!resp.ok) throw new Error("Lỗi khi tạo gói Zip");
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const baseName = doc.filename.replace(/\.[^/.]+$/, "");
        a.download = `${baseName}_with_images.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast("Đã tải về tệp ZIP (.md + ảnh)!");
      } catch (err) {
        alert("Không thể tải ZIP: " + err.message);
      }
    }

    function downloadMarkdownFile() {
      const doc = convertedDocs[activeDocIndex];
      if (!doc || !doc.markdown) return;
      const baseName = doc.filename.replace(/\.[^/.]+$/, "");
      const outputName = `${baseName}.md`;

      const blob = new Blob([doc.markdown], { type: 'text/markdown;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = outputName;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast(`Đang tải xuống ${outputName}`);
    }

    function exportStandaloneHtml() {
      const doc = convertedDocs[activeDocIndex];
      if (!doc || !doc.markdown) return;
      const rendered = document.getElementById('renderedMarkdownContent').innerHTML;
      const baseName = doc.filename.replace(/\.[^/.]+$/, "");

      const fullHtml = `<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${doc.title || baseName} — MarkItDown Export</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.0/github-markdown-dark.min.css">
  <style>
    body { box-sizing: border-box; min-width: 200px; max-width: 980px; margin: 0 auto; padding: 45px; background: #0d1117; color: #c9d1d9; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
    @media (max-width: 767px) { body { padding: 15px; } }
  </style>
</head>
<body class="markdown-body">
  ${rendered}
</body>
</html>`;

      const blob = new Blob([fullHtml], { type: 'text/html;charset=utf-8;' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${baseName}.html`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast(`Đã xuất tệp HTML hoàn chỉnh: ${baseName}.html`);
    }

    async function downloadAllAsZip() {
      if (!convertedDocs || convertedDocs.length === 0) return;
      showToast(`Đang đóng gói ${convertedDocs.length} tài liệu thành tệp ZIP...`);

      const batchData = convertedDocs.map(d => ({
        filename: d.filename,
        markdown: d.markdown,
        folder_name: (d.image_info && d.image_info.folder_name) ? d.image_info.folder_name : ""
      }));

      const formData = new FormData();
      formData.append('batch_json', JSON.stringify(batchData));

      try {
        const resp = await fetch('/api/download-all-zip', { method: 'POST', body: formData });
        if (!resp.ok) throw new Error("Không thể tạo tệp zip hàng loạt");
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `all_markdown_documents_${convertedDocs.length}_files.zip`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(`Đã tải về thành công ${convertedDocs.length} tài liệu!`);
      } catch (err) {
        alert("Lỗi khi tải zip: " + err.message);
      }
    }

    function resetAll() {
      convertedDocs = [];
      activeDocIndex = 0;
      document.getElementById('fileInput').value = "";
      document.getElementById('urlField').value = "";
      document.getElementById('resultsWorkspace').classList.add('hidden');
      document.getElementById('quickClearWrapper').classList.add('hidden');
      document.getElementById('errorMessageBanner').classList.add('hidden');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def serve_home():
    return HTMLResponse(content=HTML_CONTENT)


def find_available_port(start_port: int = 8000, max_attempts: int = 20) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start_port


def open_browser(url: str):
    time.sleep(1.2)
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    port = find_available_port(8000)
    local_url = f"http://127.0.0.1:{port}"

    print("=" * 65)
    print(f">> MarkItDown Studio dang chay tai: {local_url}")
    print("   - Dang tu dong mo trinh duyet web...")
    print("   - Nhan Ctrl+C de dung server bat ky luc nao.")
    print("=" * 65)

    threading.Thread(target=open_browser, args=(local_url,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
