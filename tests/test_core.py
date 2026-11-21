from __future__ import annotations

import io
import sqlite3
import struct
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from machina_deid.formats import (
    HandlerContext,
    _process_bytes,
    _strip_jpeg_metadata,
    _strip_png_metadata,
    process_file,
)
from machina_deid.identity import IdentityLedger, TextRedactor
from machina_deid.manifest import ManifestError, iter_manifest
from machina_deid.models import ManifestEntry
from machina_deid.paths import PathMapper
from machina_deid.runner import stratified_sample


def ledger_for(tmp_path: Path) -> tuple[sqlite3.Connection, IdentityLedger]:
    db = sqlite3.connect(tmp_path / "private.sqlite3")
    return db, IdentityLedger(db, tmp_path / "pseudonym.key")


def context_for(
    tmp_path: Path, entries: list[ManifestEntry]
) -> tuple[sqlite3.Connection, IdentityLedger, HandlerContext]:
    db, ledger = ledger_for(tmp_path)
    paths = PathMapper(ledger, entries)
    return db, ledger, HandlerContext(ledger, TextRedactor(ledger), paths, tmp_path)


def test_pseudonyms_are_stable_typed_and_case_insensitive(tmp_path: Path) -> None:
    db, ledger = ledger_for(tmp_path)
    person = ledger.pseudonym("person", "Jane Doe")
    assert person == ledger.pseudonym("person", " jane   doe ")
    assert person != ledger.pseudonym("company", "Jane Doe")
    ledger.close()
    db.close()

    db2, ledger2 = ledger_for(tmp_path)
    assert ledger2.pseudonym("person", "Jane Doe") == person
    ledger2.close()
    db2.close()


def test_redactor_preserves_dimensions_and_is_idempotent(tmp_path: Path) -> None:
    db, ledger = ledger_for(tmp_path)
    redactor = TextRedactor(ledger)
    source = "Author: Jane Doe\nEmail: jane@example.com\nLength=123.45 mm"
    cleaned = redactor.redact(source)
    assert "Jane Doe" not in cleaned
    assert "jane@example.com" not in cleaned
    assert "123.45 mm" in cleaned
    assert redactor.redact(cleaned) == cleaned
    ledger.close()
    db.close()


def test_image_metadata_stripping_preserves_decoded_pixels() -> None:
    Image = pytest.importorskip("PIL.Image")
    PngInfo = pytest.importorskip("PIL.PngImagePlugin").PngInfo
    source = Image.new("RGB", (3, 2), color=(12, 34, 56))

    png_info = PngInfo()
    png_info.add_text("Author", "Jane Doe")
    png = io.BytesIO()
    source.save(png, format="PNG", pnginfo=png_info)
    clean_png = _strip_png_metadata(png.getvalue())
    assert b"Jane Doe" not in clean_png
    assert Image.open(io.BytesIO(clean_png)).tobytes() == source.tobytes()

    jpeg = io.BytesIO()
    source.save(jpeg, format="JPEG")
    jpeg_with_comment = jpeg.getvalue()[:2] + b"\xff\xfe\x00\x0aJane Doe" + jpeg.getvalue()[2:]
    clean_jpeg = _strip_jpeg_metadata(jpeg_with_comment)
    assert b"Jane Doe" not in clean_jpeg
    assert (
        Image.open(io.BytesIO(clean_jpeg)).tobytes()
        == Image.open(io.BytesIO(jpeg_with_comment)).tobytes()
    )


def test_manifest_rejects_parent_paths(tmp_path: Path) -> None:
    path = tmp_path / "manifest.tsv"
    path.write_text("# path\tbytes\tmd5\turl path\n../outside.step\t1\t\tcorpus/outside.step\n")
    with pytest.raises(ManifestError):
        list(iter_manifest(str(path)))


def test_path_mapper_keeps_extensions_and_neutral_directories(tmp_path: Path) -> None:
    entry = ManifestEntry(PurePosixPath("sources/Project X/engine.step"), 1, None, "x", 2)
    db, ledger, context = context_for(tmp_path, [entry])
    mapped = context.paths.output_path(entry.path).as_posix()
    assert mapped.startswith("sources/Project-")
    assert mapped.endswith(".step")
    assert "Project X" not in mapped
    assert context.paths.rewrite_references("engine.step") == PurePosixPath(mapped).name
    assert context.paths.rewrite_references('"sources/Project X/engine.step"') == f'"{mapped}"'
    ledger.close()
    db.close()


def test_step_replaces_identity_strings_without_touching_geometry(tmp_path: Path) -> None:
    entry = ManifestEntry(PurePosixPath("parts/jane.step"), 1, None, "x", 2)
    db, ledger, context = context_for(tmp_path, [entry])
    source = (
        "ISO-10303-21;\nHEADER;\n"
        "FILE_NAME('jane.step','2026-11-20T12:00:00',('Jane Doe'),('ACME Inc'),'', '', '');\n"
        "ENDSEC;\nDATA;\n"
        "#1=PRODUCT('P-17','ACME gearbox','',(#2));\n"
        "#2=CARTESIAN_POINT('',(123.45,10.00,5.00));\n"
        "#3=CARTESIAN_POINT('',(614,555,1212));\nENDSEC;\nEND-ISO-10303-21;"
    )
    result, _notes = _process_bytes(source.encode(), "model.step", context)
    assert result is not None
    cleaned = result.decode()
    assert "Jane Doe" not in cleaned and "ACME Inc" not in cleaned and "ACME gearbox" not in cleaned
    assert "123.45,10.00,5.00" in cleaned
    assert "614,555,1212" in cleaned
    assert "PRODUCT(" in cleaned and "CARTESIAN_POINT" in cleaned
    ledger.close()
    db.close()


def test_zip_keeps_unsupported_binary_members_and_reports_them(tmp_path: Path) -> None:
    entry = ManifestEntry(PurePosixPath("archives/source.zip"), 1, None, "x", 2)
    db, ledger, context = context_for(tmp_path, [entry])
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("Jane Doe/notes.txt", "Author: Jane Doe\n")
    cleaned, _notes = _process_bytes(zipped.getvalue(), "source.zip", context)
    assert cleaned is not None
    with zipfile.ZipFile(io.BytesIO(cleaned)) as archive:
        names = archive.namelist()
        assert not any("Jane Doe" in name for name in names)
        assert b"Jane Doe" not in archive.read(names[0])

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("native.sldprt", b"\x00opaque")
    kept, unsafe_notes = _process_bytes(unsafe.getvalue(), "unsafe.zip", context)
    assert kept is not None
    assert "unprocessed_binary" in unsafe_notes
    with zipfile.ZipFile(io.BytesIO(kept)) as archive:
        [native_name] = archive.namelist()
        assert native_name.endswith(".sldprt")
        assert archive.read(native_name) == b"\x00opaque"
    ledger.close()
    db.close()


def test_malformed_tar_gz_is_omitted_instead_of_copied_raw(tmp_path: Path) -> None:
    entry = ManifestEntry(PurePosixPath("archives/source.tar.gz"), 1, None, "x", 2)
    db, ledger, context = context_for(tmp_path, [entry])
    source = tmp_path / "broken.tar.gz"
    destination = tmp_path / "output.tar.gz"
    source.write_bytes(b"not a tar archive")
    result = process_file(source, destination, context, logical_name="source.tar.gz")
    assert result.status == "omitted"
    assert not destination.exists()
    ledger.close()
    db.close()


def test_binary_stl_redacts_header_without_changing_triangles(tmp_path: Path) -> None:
    entry = ManifestEntry(PurePosixPath("parts/Jane Doe.stl"), 1, None, "x", 2)
    db, ledger, context = context_for(tmp_path, [entry])
    triangle = struct.pack("<12fH", *([1.25, 2.5, 3.75] * 4), 0)
    source = b"Jane Doe".ljust(80, b"\x00") + struct.pack("<I", 1) + triangle
    cleaned, notes = _process_bytes(source, "Jane Doe.stl", context)
    assert cleaned is not None and not notes
    assert b"Jane Doe" not in cleaned[:80]
    assert cleaned[80:] == source[80:]
    ledger.close()
    db.close()


def test_ascii_stl_redacts_only_solid_name_lines(tmp_path: Path) -> None:
    entry = ManifestEntry(PurePosixPath("parts/Jane Doe.stl"), 1, None, "x", 2)
    db, ledger, context = context_for(tmp_path, [entry])
    geometry = b"  vertex 123.45 10.0 5.0\r\n"
    source = b"solid Jane Doe\r\n" + geometry + b"endsolid Jane Doe\r\n"
    cleaned, notes = _process_bytes(source, "Jane Doe.stl", context)
    assert cleaned is not None and not notes
    assert b"Jane Doe" not in cleaned
    assert geometry in cleaned
    ledger.close()
    db.close()


def test_pdf_text_metadata_and_attachment_contents_are_cleaned(tmp_path: Path) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    source_doc = pymupdf.open()
    page = source_doc.new_page()
    page.insert_text((72, 72), "Owner: Jane Doe")
    source_doc.set_metadata({"author": "Jane Doe", "title": "Acme Project"})
    source_doc.embfile_add("notes.txt", b"Author: Jane Doe\nEmail: jane@example.com")
    source_bytes = source_doc.tobytes()
    source_doc.close()

    entry = ManifestEntry(PurePosixPath("docs/drawing.pdf"), 1, None, "x", 2)
    db, ledger, context = context_for(tmp_path, [entry])
    cleaned_bytes, notes = _process_bytes(source_bytes, "drawing.pdf", context)
    assert cleaned_bytes is not None
    cleaned = pymupdf.open(stream=cleaned_bytes, filetype="pdf")
    page_text = "\n".join(page.get_text() for page in cleaned)
    assert "Jane Doe" not in page_text
    assert "jane@example.com" not in cleaned.metadata["author"]
    attachment = cleaned.embfile_get(0).decode()
    assert "Jane Doe" not in attachment and "jane@example.com" not in attachment, notes
    assert "pdf_attachment_tree_key_may_retain_source_name" in notes
    cleaned.close()
    ledger.close()
    db.close()


def test_xml_identity_tags_and_format_required_archive_names(tmp_path: Path) -> None:
    entry = ManifestEntry(PurePosixPath("archives/package.docx"), 1, None, "x", 2)
    db, ledger, context = context_for(tmp_path, [entry])
    source_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<cp:coreProperties xmlns:cp="urn:core" xmlns:dc="urn:dc">'
        "<dc:creator>Jane Doe</dc:creator><dc:title>ACME Gearbox</dc:title>"
        "<cp:lastModifiedBy>Jane Doe</cp:lastModifiedBy></cp:coreProperties>"
    )
    cleaned_xml, _ = _process_bytes(source_xml.encode(), "core.xml", context)
    assert cleaned_xml is not None
    assert b"Jane Doe" not in cleaned_xml and b"ACME Gearbox" not in cleaned_xml
    import xml.etree.ElementTree as ET

    ET.fromstring(cleaned_xml)

    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types><Default Extension='xml'/></Types>")
        archive.writestr("word/document.xml", source_xml)
        archive.writestr("word/_rels/document.xml.rels", "<Relationships/>")
    cleaned_zip, _ = _process_bytes(zipped.getvalue(), "package.docx", context)
    assert cleaned_zip is not None
    with zipfile.ZipFile(io.BytesIO(cleaned_zip)) as archive:
        assert "[Content_Types].xml" in archive.namelist()
        assert "word/document.xml" in archive.namelist()
        assert "word/_rels/document.xml.rels" in archive.namelist()
        assert b"Jane Doe" not in archive.read("word/document.xml")
    ledger.close()
    db.close()


def test_stratified_sample_covers_common_formats() -> None:
    extensions = [".step", ".sat", ".pdf", ".sldprt", ".step", ".step", ".pdf"]
    entries = [
        ManifestEntry(PurePosixPath(f"folder/f{i}{ext}"), 1, None, "x", i + 1)
        for i, ext in enumerate(extensions)
    ]
    sampled = stratified_sample(entries, 4)
    assert len(sampled) == 4
    assert len({entry.path.suffix for entry in sampled}) == 4
