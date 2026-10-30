def escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_text_pdf(pages: list[list[str]]) -> bytes:
    page_ids: list[int] = []
    font_id = 3
    next_id = 4
    page_objects: list[tuple[int, bytes]] = []
    for lines in pages:
        content_lines = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
        for line in lines:
            content_lines.append(f"({escape_pdf_text(line)}) Tj T*")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("latin-1")
        content_id, page_id = next_id, next_id + 1
        next_id += 2
        page_objects.append((content_id, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        page_objects.append((page_id, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()))
        page_ids.append(page_id)
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects_by_id = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    objects_by_id.update(dict(page_objects))
    output = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id in sorted(objects_by_id):
        offsets[object_id] = len(output)
        output += f"{object_id} 0 obj\n".encode() + objects_by_id[object_id] + b"\nendobj\n"
    xref_offset = len(output)
    count = max(objects_by_id) + 1
    output += f"xref\n0 {count}\n0000000000 65535 f \n".encode()
    for object_id in range(1, count):
        output += f"{offsets[object_id]:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    return bytes(output)


def make_blank_pdf(page_count: int = 1) -> bytes:
    return make_text_pdf([[] for _ in range(page_count)])
