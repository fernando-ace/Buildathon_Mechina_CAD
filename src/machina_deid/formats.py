from __future__ import annotations

import codecs
import copy
import gzip
import html
import io
import os
import re
import shutil
import struct
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .identity import IdentityLedger, TextRedactor
from .models import FileResult
from .paths import NEUTRAL_DIRS, NEUTRAL_NAMES, PathMapper, rewrite_reference_text

ReferenceMap = Mapping[str, str] | list[tuple[str, str]]

TEXT_EXTENSIONS = {
    ".step",
    ".stp",
    ".iges",
    ".igs",
    ".sat",
    ".ifc",
    ".dxf",
    ".svg",
    ".wrl",
    ".vrml",
    ".obj",
    ".mtl",
    ".stl",
    ".3mf",
    ".xml",
    ".json",
    ".csv",
    ".txt",
    ".md",
    ".html",
    ".htm",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".log",
    ".eml",
    ".vcf",
    ".ics",
    ".prj",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PRECOMPRESSED_EXTENSIONS = {
    ".3mf",
    ".bmp",
    ".docx",
    ".fcstd",
    ".gz",
    ".jpeg",
    ".jpg",
    ".kmz",
    ".pdf",
    ".png",
    ".pptx",
    ".tif",
    ".tiff",
    ".webp",
    ".xlsx",
    ".zip",
}
ARCHIVE_EXTENSIONS = {".zip", ".fcstd", ".3mf", ".docx", ".xlsx", ".pptx", ".kmz"}
BINARY_CAD = {
    ".sldprt",
    ".sldasm",
    ".slddrw",
    ".ipt",
    ".iam",
    ".jt",
    ".dwg",
    ".f3d",
    ".prt",
    ".asm",
}
MAX_TEXT_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
STEP_STRING = re.compile(r"'(?:[^']|'')*'")
MODEL_TAG = re.compile(rb"(?is)<(?P<tag>object|component|base)\b[^>]*>")
MODEL_ATTRIBUTE = re.compile(
    rb"(?is)(?P<prefix>\b(?:name|path|filename|source|author|creator|designer|engineer|"
    rb"manufacturer|supplier|customer|title|email|phone|address|location|company|project)\s*=\s*)"
    rb"(?P<quote>['\"])(?P<value>.*?)(?P=quote)"
)
MODEL_METADATA = re.compile(
    rb"(?is)(?P<open><metadata\b[^>]*>)(?P<value>.*?)(?P<close></metadata\s*>)"
)
MODEL_METADATA_NAME = re.compile(rb"(?is)\bname\s*=\s*(['\"])(.*?)\1")


class UnsafeArchive(ValueError):
    pass


class HandlerContext:
    def __init__(
        self,
        ledger: IdentityLedger,
        redactor: TextRedactor,
        paths: PathMapper,
        scratch_dir: Path,
        max_archive_expanded_bytes: int = MAX_ARCHIVE_EXPANDED_BYTES,
    ) -> None:
        self.ledger = ledger
        self.redactor = redactor
        self.paths = paths
        self.scratch_dir = scratch_dir
        self.max_archive_expanded_bytes = max_archive_expanded_bytes
        self.notes: list[str] = []
        self._pymupdf = None
        self._pil = None
        self._pytesseract = None
        self._tesseract_available: bool | None = None

    def has_tesseract(self) -> bool:
        if self._tesseract_available is None:
            self._tesseract_available = shutil.which("tesseract") is not None
        return self._tesseract_available

    def optional(self, module: str):
        try:
            if module == "pymupdf":
                if self._pymupdf is None:
                    import pymupdf

                    self._pymupdf = pymupdf
                return self._pymupdf
            if module == "PIL":
                if self._pil is None:
                    from PIL import Image, ImageDraw, ImageFont

                    self._pil = (Image, ImageDraw, ImageFont)
                return self._pil
            if module == "pytesseract":
                if self._pytesseract is None:
                    import pytesseract

                    self._pytesseract = pytesseract
                return self._pytesseract
        except ImportError:
            return None
        return None

    def reference_rewrite(self, text: str, references: ReferenceMap | None) -> str:
        if references is None:
            return self.paths.rewrite_references(text)
        if not references:
            return text
        reference_map = references if isinstance(references, Mapping) else dict(references)
        return rewrite_reference_text(text, reference_map)


def _pseudo_member_name(name: str, context: HandlerContext) -> str:
    path = PurePosixPath(name)
    if path.name in {"", "."}:
        return name
    mapped: list[str] = []
    for component in path.parts[:-1]:
        mapped.append(
            component
            if component.casefold() in NEUTRAL_DIRS
            else context.ledger.pseudonym("project", component)
        )
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    structural = bool(
        re.fullmatch(r"(?:sheet|slide|chart|theme|image|media)\d+", stem, re.IGNORECASE)
    )
    new_stem = (
        stem
        if stem.casefold() in NEUTRAL_NAMES or structural
        else context.ledger.pseudonym("part", stem)
    )
    mapped.append(new_stem + suffix)
    return PurePosixPath(*mapped).as_posix()


def _process_text(
    data: bytes, context: HandlerContext, references: ReferenceMap | None, logical_name: str
) -> bytes:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        try:
            data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "cp1252"
    text = data.decode(encoding, errors="replace")
    suffix = PurePosixPath(logical_name).suffix.lower()
    if suffix in {".step", ".stp", ".p21"}:
        text = _redact_step(text, context, references)
        text = _rewrite_step_references(text, context, references)
        return text.encode(encoding, errors="replace")
    if suffix in {".xml", ".rels"}:
        note_count = len(context.notes)
        text = _redact_xml(text, context, references)
        if len(context.notes) == note_count:
            return text.encode(encoding, errors="replace")
    text = context.redactor.redact(text)
    text = context.reference_rewrite(text, references)
    return text.encode(encoding, errors="replace")


def _redact_3mf_model(
    data: bytes, context: HandlerContext, references: ReferenceMap | None
) -> bytes:
    """Redact 3MF identity fields without scanning numeric mesh geometry."""

    text = data

    def rewrite_tag(match: re.Match[bytes]) -> bytes:
        tag = match.group("tag").decode("ascii").casefold()

        def rewrite_attribute(attribute: re.Match[bytes]) -> bytes:
            raw_name = attribute.group("prefix").split(b"=", 1)[0].strip().decode("ascii")
            name = raw_name.casefold()
            quote = attribute.group("quote")
            value = html.unescape(attribute.group("value").decode("utf-8"))
            if name == "name" and tag in {"object", "component", "base"}:
                cleaned = context.ledger.pseudonym("part", value)
            elif name in {"path", "filename", "source"}:
                cleaned = context.reference_rewrite(context.redactor.redact(value), references)
            else:
                identity = _xml_kind(name)
                if identity is None:
                    return attribute.group(0)
                cleaned = _xml_identity_value(value, identity[1], context)
            return (
                attribute.group("prefix")
                + quote
                + html.escape(cleaned, quote=False).encode("utf-8")
                + quote
            )

        return MODEL_ATTRIBUTE.sub(rewrite_attribute, match.group(0))

    text = MODEL_TAG.sub(rewrite_tag, text)

    def rewrite_metadata(match: re.Match[bytes]) -> bytes:
        opening = match.group("open")
        name_match = MODEL_METADATA_NAME.search(opening)
        value = html.unescape(match.group("value").decode("utf-8"))
        if name_match:
            name = html.unescape(name_match.group(2).decode("utf-8"))
        else:
            name = ""
        identity = _xml_kind(name)
        if identity is not None:
            cleaned = _xml_identity_value(value, identity[1], context)
        else:
            cleaned = context.reference_rewrite(context.redactor.redact(value), references)
        return (
            opening
            + html.escape(cleaned, quote=False).encode("utf-8")
            + match.group("close")
        )

    text = MODEL_METADATA.sub(rewrite_metadata, text)
    return text


def _xml_kind(tag: str) -> tuple[str, str] | None:
    name = tag.rsplit("}", 1)[-1].split(":")[-1].casefold().replace("_", "")
    mappings = {
        "author": ("person", "Author"),
        "creator": ("person", "Creator"),
        "lastmodifiedby": ("person", "Author"),
        "createdby": ("person", "Created by"),
        "designer": ("person", "Designer"),
        "engineer": ("person", "Engineer"),
        "person": ("person", "Person"),
        "contact": ("person", "Contact"),
        "email": ("account", "Email"),
        "phone": ("contact", "Phone"),
        "address": ("address", "Address"),
        "location": ("address", "Location"),
        "company": ("company", "Company"),
        "organization": ("company", "Organization"),
        "organisation": ("company", "Organisation"),
        "manufacturer": ("company", "Company"),
        "supplier": ("company", "Supplier"),
        "customer": ("company", "Customer"),
        "project": ("project", "Project"),
        "projectname": ("project", "Project"),
        "part": ("part", "Part"),
        "partname": ("part", "Part"),
        "partnumber": ("part", "Part number"),
        "title": ("project", "Project"),
        "domain": ("domain", "Domain"),
        "account": ("account", "Account"),
    }
    return mappings.get(name)


def _xml_identity_value(value: str, label: str, context: HandlerContext) -> str:
    prefix = value[: len(value) - len(value.lstrip())]
    suffix = value[len(value.rstrip()) :]
    core = value.strip()
    if not core:
        return value
    cleaned = context.redactor.redact(f"{label}: {core}")
    return prefix + cleaned.split(":", 1)[1].strip() + suffix


def _redact_xml(text: str, context: HandlerContext, references: ReferenceMap | None) -> str:
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        root = ET.fromstring(text, parser=parser)
    except ET.ParseError:
        context.notes.append("xml_parse_failed")
        return text
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        identity = _xml_kind(node.tag)
        parent_name = node.tag.rsplit("}", 1)[-1].casefold()
        if parent_name == "metadata":
            metadata_name = next(
                (value for key, value in node.attrib.items() if key.casefold() == "name"), ""
            )
            identity = _xml_kind(metadata_name) or identity
        if identity and node.text:
            node.text = _xml_identity_value(node.text, identity[1], context)
        for key, value in list(node.attrib.items()):
            attribute = _xml_kind(key)
            if key.casefold() == "name" and parent_name == "metadata":
                continue
            if (
                attribute is None
                and key.casefold() == "name"
                and parent_name in {"object", "component", "part", "project"}
            ):
                attribute = (
                    "part" if parent_name in {"object", "component", "part"} else "project",
                    "Part" if parent_name in {"object", "component", "part"} else "Project",
                )
            if attribute:
                node.attrib[key] = _xml_identity_value(value, attribute[1], context)
            else:
                node.attrib[key] = context.reference_rewrite(
                    context.redactor.redact(value), references
                )
        if node.text:
            node.text = context.reference_rewrite(context.redactor.redact(node.text), references)
        if node.tail:
            node.tail = context.reference_rewrite(context.redactor.redact(node.tail), references)
    try:
        declaration = re.match(r"\s*(<\?xml[^?]*\?>)", text)
        encoded = ET.tostring(root, encoding="unicode", xml_declaration=False)
        return (declaration.group(1) + "\n" if declaration else "") + encoded
    except (ET.ParseError, TypeError, ValueError):
        context.notes.append("xml_serialization_failed")
        return text


def _step_args(source: str) -> list[tuple[int, int, str]]:
    """Return top-level argument spans from a STEP entity body."""

    spans: list[tuple[int, int, str]] = []
    depth = 0
    quoted = False
    start = 0
    index = 0
    while index < len(source):
        char = source[index]
        if char == "'":
            if quoted and index + 1 < len(source) and source[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                spans.append((start, index, source[start:index]))
                start = index + 1
        index += 1
    spans.append((start, len(source), source[start:]))
    return spans


def _step_pseudonym_strings(fragment: str, kind: str, context: HandlerContext) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)[1:-1].replace("''", "'")
        if not raw.strip():
            return match.group(0)
        pseudo = context.ledger.pseudonym(kind, raw)
        return "'" + pseudo.replace("'", "''") + "'"

    return STEP_STRING.sub(replace, fragment)


def _redact_step(text: str, context: HandlerContext, references: ReferenceMap | None) -> str:
    """Edit STEP identity-bearing header/product strings, never numeric geometry."""

    entity_re = re.compile(
        r"(?is)\b(FILE_NAME|PRODUCT|PRODUCT_DEFINITION|PRODUCT_DEFINITION_FORMATION)\s*\((.*?)\);"
    )

    def edit_entity(match: re.Match[str]) -> str:
        name = match.group(1).upper()
        body = match.group(2)
        args = _step_args(body)
        edits: list[tuple[int, int, str]] = []
        for index, (start, end, argument) in enumerate(args):
            kind = None
            if name == "FILE_NAME":
                if index == 0:
                    replacement = context.reference_rewrite(argument, references)
                    if replacement != argument:
                        edits.append((start, end, replacement))
                elif index == 2:
                    kind = "person"
                elif index == 3:
                    kind = "company"
            elif (
                name in {"PRODUCT", "PRODUCT_DEFINITION", "PRODUCT_DEFINITION_FORMATION"}
                and index < 2
            ):
                kind = "part"
            if kind:
                replacement = _step_pseudonym_strings(argument, kind, context)
                if name == "FILE_NAME" and index == 0:
                    replacement = context.reference_rewrite(replacement, references)
                edits.append((start, end, replacement))
        for start, end, replacement in reversed(edits):
            body = body[:start] + replacement + body[end:]
        return f"{match.group(1)}({body});"

    return entity_re.sub(edit_entity, text)


def _rewrite_step_references(
    text: str, context: HandlerContext, references: ReferenceMap | None
) -> str:
    """Rewrite linked filenames in STEP string literals without scanning numeric geometry."""

    return STEP_STRING.sub(
        lambda match: context.reference_rewrite(match.group(0), references),
        text,
    )


def _is_unsupported_binary(name: str, data: bytes) -> bool:
    ext = PurePosixPath(name).suffix.lower()
    if ext in BINARY_CAD:
        return True
    # Unknown non-text members are unsafe to keep inside an archive: they may
    # retain the original filename or private native-file properties.
    sample = data[:4096]
    return b"\x00" in sample and ext not in IMAGE_EXTENSIONS


def _process_zip(data: bytes, context: HandlerContext) -> tuple[bytes, list[str]]:
    source = io.BytesIO(data)
    destination = io.BytesIO()
    notes: list[str] = []
    with zipfile.ZipFile(source, "r") as zin:
        members = [info for info in zin.infolist() if not info.is_dir()]
        total = sum(info.file_size for info in members)
        if total > context.max_archive_expanded_bytes:
            raise UnsafeArchive("archive_expansion_limit")
        names = {
            info.filename: _pseudo_member_name(info.filename, context) for info in zin.infolist()
        }
        references = {old: new for old, new in names.items() if old != new}
        with zipfile.ZipFile(destination, "w", compresslevel=1) as zout:
            zout.comment = context.redactor.redact(zin.comment.decode("utf-8", "replace")).encode(
                "utf-8"
            )[:65535]
            for info in zin.infolist():
                new_info = copy.copy(info)
                new_info.filename = names[info.filename]
                new_info.orig_filename = new_info.filename
                if PurePosixPath(info.filename).suffix.lower() in PRECOMPRESSED_EXTENSIONS:
                    new_info.compress_type = zipfile.ZIP_STORED
                new_info.extra = (
                    b""  # Remove raw Unicode path/comment metadata; ZIP64 is regenerated if needed.
                )
                new_info.comment = context.redactor.redact(
                    info.comment.decode("utf-8", "replace")
                ).encode("utf-8")
                if info.is_dir():
                    zout.writestr(new_info, b"")
                    continue
                if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise UnsafeArchive("archive_member_limit")
                raw = zin.read(info)
                processed, member_notes = _process_bytes(raw, info.filename, context, references)
                if processed is None:
                    raise UnsafeArchive("archive_contains_unprocessed_member")
                notes.extend(member_notes)
                zout.writestr(new_info, processed)
    return destination.getvalue(), sorted(set(notes))


def _process_tar(data: bytes, name: str, context: HandlerContext) -> tuple[bytes, list[str]]:
    source = io.BytesIO(data)
    destination = io.BytesIO()
    compressed = name.lower().endswith((".gz", ".tgz"))
    mode = "w:gz" if compressed else "w"
    notes: list[str] = []
    with tarfile.open(fileobj=source, mode="r:*") as tin:
        members = tin.getmembers()
        total = sum(member.size for member in members if member.isfile())
        if total > context.max_archive_expanded_bytes:
            raise UnsafeArchive("archive_expansion_limit")
        names = {member.name: _pseudo_member_name(member.name, context) for member in members}
        references = {old: new for old, new in names.items() if old != new}
        with tarfile.open(fileobj=destination, mode=mode) as tout:
            for member in members:
                out_info = copy.copy(member)
                out_info.name = names[member.name]
                out_info.pax_headers = {
                    key: context.redactor.redact(value)
                    for key, value in member.pax_headers.items()
                    if key not in {"path", "linkpath", "uname", "gname"}
                }
                if member.uname:
                    out_info.uname = context.ledger.pseudonym("person", member.uname)
                if member.gname:
                    out_info.gname = context.ledger.pseudonym("person", member.gname)
                if member.linkname:
                    out_info.linkname = context.reference_rewrite(member.linkname, references)
                if member.isfile():
                    if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise UnsafeArchive("archive_member_limit")
                    extracted = tin.extractfile(member)
                    if extracted is None:
                        raise UnsafeArchive("archive_member_unreadable")
                    raw = extracted.read()
                    processed, member_notes = _process_bytes(raw, member.name, context, references)
                    if processed is None:
                        raise UnsafeArchive("archive_contains_unprocessed_member")
                    notes.extend(member_notes)
                    tout.addfile(out_info, io.BytesIO(processed))
                else:
                    tout.addfile(out_info)
    return destination.getvalue(), sorted(set(notes))


def _process_image(data: bytes, context: HandlerContext) -> tuple[bytes, list[str]]:
    tesseract_available = context.has_tesseract()
    if not tesseract_available and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _strip_png_metadata(data), ["visible_text_not_scanned_tesseract_unavailable"]
    if not tesseract_available and data.startswith(b"\xff\xd8"):
        return _strip_jpeg_metadata(data), ["visible_text_not_scanned_tesseract_unavailable"]
    modules = context.optional("PIL")
    if not modules:
        return data, ["image_metadata_not_processed_pillow_missing", "visible_text_not_scanned"]
    Image, ImageDraw, ImageFont = modules
    image = Image.open(io.BytesIO(data))
    image.load()
    notes: list[str] = []
    try:
        pytesseract = context.optional("pytesseract")
        if pytesseract is None or not tesseract_available:
            notes.append("visible_text_not_scanned_tesseract_unavailable")
        else:
            ocr = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
            groups: dict[tuple[int, int, int], list[int]] = {}
            for index, word in enumerate(ocr.get("text", [])):
                if word.strip():
                    key = (ocr["block_num"][index], ocr["par_num"][index], ocr["line_num"][index])
                    groups.setdefault(key, []).append(index)
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default()
            for indices in groups.values():
                line = " ".join(ocr["text"][index] for index in indices)
                clean = context.redactor.redact(line)
                if clean == line:
                    continue
                x0 = min(ocr["left"][index] for index in indices)
                y0 = min(ocr["top"][index] for index in indices)
                x1 = max(ocr["left"][index] + ocr["width"][index] for index in indices)
                y1 = max(ocr["top"][index] + ocr["height"][index] for index in indices)
                draw.rectangle((x0, y0, x1, y1), fill="white")
                draw.text((x0, y0), clean, fill="black", font=font)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, IndexError):
        notes.append("visible_text_ocr_failed")
    output = io.BytesIO()
    fmt = image.format or "PNG"
    save_args = {"format": fmt}
    if fmt.upper() in {"JPEG", "JPG"}:
        save_args["quality"] = 95
    image.save(output, **save_args)  # EXIF and other source metadata are intentionally omitted.
    return output.getvalue(), notes


def _strip_png_metadata(data: bytes) -> bytes:
    """Remove PNG metadata chunks without decoding or recompressing pixels."""

    metadata = {b"eXIf", b"iCCP", b"iTXt", b"pHYs", b"tEXt", b"tIME", b"zTXt"}
    output = bytearray(data[:8])
    offset = 8
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            return data
        kind = data[offset + 4 : offset + 8]
        if kind not in metadata:
            output.extend(data[offset:end])
        offset = end
        if kind == b"IEND":
            return bytes(output) + data[offset:]
    return data


def _strip_jpeg_metadata(data: bytes) -> bytes:
    """Remove JPEG application/comment segments while preserving compressed scans."""

    if not data.startswith(b"\xff\xd8"):
        return data
    output = bytearray(data[:2])
    offset = 2
    while offset < len(data):
        if data[offset] != 0xFF:
            return data
        marker_start = offset
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return data
        marker = data[offset]
        offset += 1
        if marker == 0xDA:
            output.extend(data[marker_start:])
            return bytes(output)
        if marker in {0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)}:
            output.extend(data[marker_start:offset])
            if marker == 0xD9:
                output.extend(data[offset:])
                return bytes(output)
            continue
        if offset + 2 > len(data):
            return data
        length = struct.unpack_from(">H", data, offset)[0]
        end = offset + length
        if length < 2 or end > len(data):
            return data
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            output.extend(data[marker_start:end])
        offset = end
    return data


def _process_pdf(
    data: bytes, context: HandlerContext, references: ReferenceMap | None
) -> tuple[bytes, list[str]]:
    pymupdf = context.optional("pymupdf")
    if pymupdf is None:
        raise ValueError("pymupdf_missing")
    doc = pymupdf.open(stream=data, filetype="pdf")
    notes: list[str] = []
    metadata = doc.metadata or {}
    for key in ("title", "author", "subject", "keywords", "creator", "producer"):
        value = metadata.get(key)
        if value:
            value = str(value)
            if key == "author":
                value = context.redactor.redact("Author: " + value).split(":", 1)[1].strip()
            elif key in {"title", "subject", "keywords"}:
                value = context.redactor.redact("Project: " + value).split(":", 1)[1].strip()
            else:
                value = context.redactor.redact(value)
            metadata[key] = context.reference_rewrite(value, references)
    doc.set_metadata(metadata)
    try:
        xmp = doc.get_xml_metadata()
        if xmp:
            doc.set_xml_metadata(
                context.reference_rewrite(context.redactor.redact(xmp), references)
            )
    except (RuntimeError, ValueError, TypeError):
        notes.append("pdf_xmp_metadata_not_updated")

    tesseract_available = context.has_tesseract()
    ocr_dpi = 150 if len(data) >= 64 * 1024**2 or doc.page_count > 50 else 200
    if ocr_dpi < 200:
        notes.append("pdf_large_document_ocr_reduced_dpi")
    for page in doc:
        textpage = None
        page_has_redactions = False
        native_text = page.get_text("text")
        has_images = bool(page.get_images(full=True))
        needs_ocr = has_images or len(native_text.strip()) < 80
        if tesseract_available and needs_ocr:
            try:
                textpage = page.get_textpage_ocr(
                    flags=0, language="eng", dpi=ocr_dpi, full=False
                )
            except (OSError, RuntimeError, ValueError, TypeError):
                notes.append("pdf_ocr_failed")
        elif not tesseract_available and needs_ocr:
            notes.append("pdf_scanned_text_not_ocr_scanned")
        try:
            content = (
                page.get_text("dict", textpage=textpage) if textpage else page.get_text("dict")
            )
            for block in content.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        original = str(span.get("text", ""))
                        cleaned = context.reference_rewrite(
                            context.redactor.redact(original), references
                        )
                        if cleaned != original:
                            rect = pymupdf.Rect(span["bbox"])
                            page.add_redact_annot(
                                rect,
                                text=cleaned,
                                fontname="helv",
                                fontsize=max(5, min(float(span.get("size", 8)), 10)),
                                fill=(1, 1, 1),
                                text_color=(0, 0, 0),
                            )
                            page_has_redactions = True
        except (RuntimeError, ValueError, TypeError, KeyError):
            notes.append("pdf_page_text_not_fully_processed")
        try:
            for link in page.get_links():
                uri = link.get("uri")
                if uri:
                    cleaned = context.reference_rewrite(context.redactor.redact(uri), references)
                    if cleaned != uri:
                        link["uri"] = cleaned
                        page.update_link(link)
        except Exception:  # noqa: BLE001 - PyMuPDF raises base Exception for malformed link xrefs.
            notes.append("pdf_links_not_fully_processed")
        try:
            for annot in page.annots() or []:
                info = annot.info
                changed = False
                for key in ("content", "title", "subject"):
                    value = info.get(key, "")
                    clean = context.reference_rewrite(context.redactor.redact(value), references)
                    if clean != value:
                        info[key] = clean
                        changed = True
                if changed:
                    annot.set_info(info)
                    annot.update()
        except (RuntimeError, ValueError, TypeError, AttributeError):
            notes.append("pdf_annotations_not_fully_processed")
        if page_has_redactions:
            try:
                page.apply_redactions(images=2, graphics=1, text=0)
            except TypeError:
                page.apply_redactions()

    embedded_xrefs: list[int] = []
    try:
        catalog = doc.pdf_catalog()
        _, raw_names = doc.xref_get_key(catalog, "Names/EmbeddedFiles/Names")
        embedded_xrefs = [
            int(match.group(1))
            for match in re.finditer(r"/EF\s*<<\s*/F\s+(\d+)\s+0\s+R", raw_names)
        ]
    except (RuntimeError, ValueError, TypeError, AttributeError, IndexError):
        notes.append("pdf_attachment_stream_index_unavailable")
    for index, name in enumerate(list(doc.embfile_names())):
        try:
            info = doc.embfile_info(name)
            raw = doc.embfile_get(name)
            cleaned, attached_notes = _process_bytes(raw, name, context, references)
            if cleaned is None or "unprocessed_binary" in attached_notes:
                notes.append("pdf_attachment_unprocessed")
                continue
            new_name = _pseudo_member_name(name, context)
            if index >= len(embedded_xrefs):
                notes.append("pdf_attachment_content_not_updated")
                continue
            doc.update_stream(embedded_xrefs[index], cleaned, compress=1)
            doc.embfile_upd(
                index,
                filename=new_name,
                ufilename=new_name,
                desc=context.redactor.redact(str(info.get("description", ""))),
            )
            if new_name != name:
                notes.append("pdf_attachment_tree_key_may_retain_source_name")
            notes.extend(attached_notes)
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            IndexError,
        ) as exc:
            notes.append("pdf_attachment_not_fully_processed_" + type(exc).__name__)

    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    return output.getvalue(), sorted(set(notes))


def _process_bytes(
    data: bytes,
    name: str,
    context: HandlerContext,
    references: ReferenceMap | None = None,
) -> tuple[bytes | None, list[str]]:
    ext = PurePosixPath(name).suffix.lower()
    try:
        if name.lower().endswith((".tar", ".tar.gz", ".tgz")) or data[257:262] == b"ustar":
            return _process_tar(data, name, context)
        if (data.startswith(b"PK\x03\x04") or ext in ARCHIVE_EXTENSIONS) and zipfile.is_zipfile(
            io.BytesIO(data)
        ):
            return _process_zip(data, context)
        if data.startswith(b"%PDF-") or ext == ".pdf":
            return _process_pdf(data, context, references)
        if ext == ".model" and data.lstrip().startswith(b"<?xml"):
            return _redact_3mf_model(data, context, references), []
        if ext in IMAGE_EXTENSIONS:
            return _process_image(data, context)
        if ext == ".stl":
            binary_header = _binary_stl_header(data)
            if binary_header is not None:
                header, notes = _redact_binary_stl_header(binary_header, context)
                if header == binary_header:
                    return data, notes
                return header + data[80:], notes
            return _redact_ascii_stl(data, context), []
        if ext in TEXT_EXTENSIONS:
            return _process_text(data, context, references, name), []
        if data.startswith(b"\x1f\x8b") or ext in {".gz", ".tgz"}:
            try:
                uncompressed = gzip.decompress(data)
                inner_name = name[:-3] if name.lower().endswith(".gz") else name + ".tar"
                if (
                    inner_name.lower().endswith((".tar", ".tar.gz", ".tgz"))
                    or uncompressed[257:262] == b"ustar"
                ):
                    return _process_tar(data, name, context)
                processed, notes = _process_bytes(uncompressed, inner_name, context, references)
                if processed is None:
                    return None, notes
                output = io.BytesIO()
                with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as stream:
                    stream.write(processed)
                return output.getvalue(), notes
            except (OSError, EOFError, gzip.BadGzipFile):
                return data, ["gzip_archive_unreadable", "unprocessed_binary"]
        if ext in BINARY_CAD or _is_unsupported_binary(name, data):
            return data, ["unprocessed_binary"]
        # Unknown text-like engineering formats receive conservative contact and
        # labeled-field replacement without changing non-text binary payloads.
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            return data, ["unknown_binary_format", "unprocessed_binary"]
        return context.reference_rewrite(context.redactor.redact(decoded), references).encode(
            "utf-8"
        ), ["unknown_text_format"]
    except UnsafeArchive as exc:
        return None, [str(exc)]


def _is_archive_name(name: str) -> bool:
    return (
        name.lower().endswith((".tar", ".tar.gz", ".tgz"))
        or PurePosixPath(name).suffix.lower() in ARCHIVE_EXTENSIONS
    )


def _binary_stl_header(data: bytes) -> bytes | None:
    if len(data) < 84:
        return None
    triangle_count = struct.unpack_from("<I", data, 80)[0]
    return data[:80] if len(data) == 84 + triangle_count * 50 else None


def _redact_binary_stl_header(header: bytes, context: HandlerContext) -> tuple[bytes, list[str]]:
    if b"COLOR=" in header:
        return header, ["binary_stl_color_header_preserved"]
    text = header.decode("ascii", errors="ignore").strip("\x00 \t\r\n")
    if len(text) < 4 or not re.search(r"[A-Za-z]{3}", text):
        return header, ["binary_stl_header_not_readable"]
    pseudonym = context.ledger.pseudonym("part", text).encode("ascii")
    return pseudonym[:80].ljust(80, b"\x00"), []


_ASCII_STL_NAME = re.compile(rb"(?i)^(solid|endsolid)([ \t]+)([^\r\n]*)(\r?\n)?$")


def _redact_ascii_stl(data: bytes, context: HandlerContext) -> bytes:
    """Change ASCII STL name lines while leaving every facet and coordinate byte intact."""

    output = bytearray()
    for line in io.BytesIO(data):
        match = _ASCII_STL_NAME.match(line)
        if match and match.group(3).strip():
            original = match.group(3).decode("utf-8", errors="replace").strip()
            replacement = context.ledger.pseudonym("part", original).encode("ascii")
            output.extend(match.group(1) + match.group(2) + replacement + (match.group(4) or b""))
        else:
            output.extend(line)
    return bytes(output)


def _process_stl_file(source: Path, destination: Path, context: HandlerContext) -> list[str]:
    size = source.stat().st_size
    with source.open("rb") as reader:
        prefix = reader.read(84)
        triangle_count = struct.unpack_from("<I", prefix, 80)[0] if len(prefix) == 84 else -1
        if triangle_count >= 0 and size == 84 + triangle_count * 50:
            header, notes = _redact_binary_stl_header(prefix[:80], context)
            reader.seek(80)
            with destination.open("wb") as writer:
                writer.write(header)
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            return notes

        reader.seek(0)
        with destination.open("wb") as writer:
            for line in reader:
                match = _ASCII_STL_NAME.match(line)
                if match and match.group(3).strip():
                    original = match.group(3).decode("utf-8", errors="replace").strip()
                    replacement = context.ledger.pseudonym("part", original).encode("ascii")
                    line = match.group(1) + match.group(2) + replacement + (match.group(4) or b"")
                writer.write(line)
    return []


def _text_encoding(prefix: bytes) -> tuple[str, bytes]:
    if prefix.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", codecs.BOM_UTF16_LE
    if prefix.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", codecs.BOM_UTF16_BE
    if prefix.startswith(codecs.BOM_UTF8):
        return "utf-8", codecs.BOM_UTF8
    try:
        prefix.decode("utf-8")
        return "utf-8", b""
    except UnicodeDecodeError:
        return "cp1252", b""


def _transform_text_chunk(
    text: str, logical_name: str, context: HandlerContext, references: ReferenceMap | None
) -> str:
    if PurePosixPath(logical_name).suffix.lower() in {".step", ".stp", ".p21"}:
        text = _redact_step(text, context, references)
        return _rewrite_step_references(text, context, references)
    return context.reference_rewrite(context.redactor.redact(text), references)


def _stream_text_file(
    source: Path, destination: Path, logical_name: str, context: HandlerContext
) -> list[str]:
    """Transform large textual CAD files with a bounded carry window."""

    references: ReferenceMap | None = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader:
        prefix = reader.read(4)
        encoding, bom = _text_encoding(prefix)
        reader.seek(len(bom))
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        carry = ""
        with destination.open("wb") as writer:
            if bom:
                writer.write(bom)
            while chunk := reader.read(1024 * 1024):
                text = carry + decoder.decode(chunk, final=False)
                if len(text) <= 8192:
                    carry = text
                    continue
                boundary = len(text) - 8192
                writer.write(
                    _transform_text_chunk(
                        text[:boundary], logical_name, context, references
                    ).encode(encoding, errors="replace")
                )
                carry = text[boundary:]
            tail = carry + decoder.decode(b"", final=True)
            if tail:
                writer.write(
                    _transform_text_chunk(tail, logical_name, context, references).encode(
                        encoding, errors="replace"
                    )
                )
    return ["large_text_streamed"]


def process_file(
    source: Path, destination: Path, context: HandlerContext, logical_name: str | None = None
) -> FileResult:
    """Transform one input file without exposing source paths in returned notes."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    temporary.unlink(missing_ok=True)

    note_start = len(context.notes)

    def finish(status: str, notes: list[str]) -> FileResult:
        context_notes = context.notes[note_start:]
        del context.notes[note_start:]
        return FileResult(status, list(dict.fromkeys([*notes, *context_notes])))

    try:
        logical_name = logical_name or source.name
        size = source.stat().st_size
        if PurePosixPath(logical_name).suffix.lower() == ".stl":
            notes = _process_stl_file(source, temporary, context)
        elif (
            size > MAX_TEXT_BYTES and PurePosixPath(logical_name).suffix.lower() in TEXT_EXTENSIONS
        ):
            notes = _stream_text_file(source, temporary, logical_name, context)
        elif size > MAX_TEXT_BYTES:
            if PurePosixPath(logical_name).suffix.lower() in ARCHIVE_EXTENSIONS:
                return finish("omitted", ["oversize_archive_not_processed"])
            shutil.copyfile(source, temporary)
            notes = ["large_binary_not_processed"]
        else:
            raw = source.read_bytes()
            result, notes = _process_bytes(raw, logical_name, context)
            if result is None:
                return finish("omitted", notes or ["format_handler_omitted_file"])
            temporary.write_bytes(result)
        os.replace(temporary, destination)
        status = "partial" if notes else "processed"
        return finish(status, notes)
    except Exception as exc:  # noqa: BLE001 - format libraries may raise base Exception for malformed files.
        temporary.unlink(missing_ok=True)
        # Do not copy an unreadable archive into corpus: nested originals would
        # remain available without any processed copy.
        if _is_archive_name(logical_name or source.name):
            return finish("omitted", ["archive_processing_failed_" + type(exc).__name__])
        try:
            shutil.copyfile(source, destination)
        except OSError:
            return finish("omitted", ["source_file_unreadable"])
        return finish("partial", ["format_processing_failed_" + type(exc).__name__])
    finally:
        temporary.unlink(missing_ok=True)
