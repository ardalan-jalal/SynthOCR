"""A local FastAPI studio over the maintained, bounded SynthOCR pipeline."""

import base64
import hashlib
import heapq
import io
import json
import ntpath
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from math import isfinite
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from anyio import CancelScope
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fontTools.ttLib import TTCollection, TTFont, TTLibError
from PIL import Image, ImageFont, features
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import (
    AUGMENTATION_FIELDS,
    DOCUMENT_LAYER_LABELS,
    DOCUMENT_PRESETS,
    IMAGE_PRESETS,
    Config,
    background_distribution,
    validate_augmentation,
)
from .corpus_store import canonical_language, resolve_source_language
from .downloads import sha256
from .fonts import FONT_SUFFIXES, Coverage, discover, font_ids, select
from .pipeline import GenerationCancelled, export, generate
from .render import Renderer
from .spelling import spelling_cases
from .text import chunks, normalize, prepare, records, required_codepoints
from .verify import dataset_files, verify

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
LOCATION = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
FOLDER_NAME = re.compile(r'[^\x00-\x1f\x7f<>:"/\\|?*]{1,80}\Z')
DIRECTORY_LIMIT = 200
DIRECTORY_SCAN_LIMIT = 2000
JOB_ID = re.compile(r"[a-f0-9]{32}\Z")
DOWNLOAD_FORMAT = Literal["dataset", "csv", "imagefolder", "tesseract", "manifest"]
LANGUAGE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z")
IMAGE = re.compile(r"[0-9]{8}\.(?:png|jpg|webp|tif)\Z")
CACHE_FILES = 128
INDEX_STRIDE = 128
UPLOAD_BYTES = 50 * 1024 * 1024
FONT_UPLOAD_BYTES = 32 * 1024 * 1024
USER_FONT_LIMIT = 200
FONT_ID = re.compile(r"[a-f0-9]{64}:[0-9]{1,4}\Z")
# A short sample of each page language; a font that draws it all is marked as
# supporting that language. Generation still checks every label separately.
LANGUAGE_SAMPLES = {
    "ckb": "ئابپتجچحخدرڕزژسشعغفڤقکگلڵمنوۆهەیێ",
    "kmr": "abcçdeêfghiîjklmnopqrsştuûvwxyzABCÇDEÊFGHIÎJKLMNOPQRSŞTUÛVWXYZ",
    "ar": "ابتثجحخدذرزسشصضطظعغفقكلمنهوي",
    "en": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
}
UPLOAD_TOTAL_BYTES = 100 * 1024 * 1024
UPLOAD_FILES = 8
UPLOAD_TTL = 24 * 60 * 60
UPLOAD_CHUNK = 64 * 1024
ACTIVE = {"queued", "preparing", "generating", "verifying", "cancelling"}
LANGUAGES = {"sorani": ["ckb"], "kurmanji": ["kmr"], "both": ["ckb", "kmr"], "all": []}
FOCUSED_LANGUAGES = ("ckb", "kmr", "ar", "en")
LanguageSelection = Annotated[
    list[Literal["ckb", "kmr", "ar", "en"]], Field(min_length=1, max_length=4)
]


def _font_display_name(font) -> str:
    try:
        with TTFont(font.path, fontNumber=font.index, lazy=True) as face:
            name = face["name"].getDebugName(4) or face["name"].getDebugName(1)
    except OSError, ValueError, KeyError, TTLibError:
        name = None
    return (name or Path(font.path).stem)[:80]


def _folder_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    try:
        if len(name.encode("utf-8")) > 255:
            return False
    except UnicodeEncodeError:
        return False
    return bool(
        FOLDER_NAME.fullmatch(name)
        and not name.startswith(".")
        and name[-1] not in " ."
        and not ntpath.isreserved(name)
    )


def _folder_parts(path: str) -> list[str]:
    if not isinstance(path, str) or len(path) > 512:
        raise ValueError("Output folder must be a relative path of at most 512 characters")
    parts = path.split("/") if path else []
    if len(parts) > 8 or any(not _folder_name(part) for part in parts):
        raise ValueError("Choose an output folder within the selected storage location")
    return parts


class DirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location: str = Field(default="default", pattern=r"^[a-z][a-z0-9-]{0,31}$")
    path: str = Field(default="", max_length=512)
    name: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def valid_directory(self):
        _folder_parts(self.path)
        if not _folder_name(self.name):
            raise ValueError("Use a portable folder name without path separators or reserved names")
        _folder_parts("/".join(filter(None, (self.path, self.name))))
        return self


class AppearanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    augmentation: float = Field(default=0.35, ge=0, le=1, allow_inf_nan=False, strict=True)
    background: Literal["paper", "plain", "gradient", "noise"] = "paper"
    color_mode: Literal["grayscale", "rgb"] = "grayscale"
    background_color: str = "#f5f1e8"
    background_color_end: str = "#e4e9ed"
    text_color: str = "#1a1a1a"
    texture_strength: float = Field(default=0.04, ge=0, le=0.25, allow_inf_nan=False, strict=True)
    rotation: float = Field(default=2, ge=0, le=10, allow_inf_nan=False, strict=True)
    blur: float = Field(default=0.55, ge=0, le=2, allow_inf_nan=False, strict=True)
    contrast: float = Field(default=0.1, ge=0, le=0.5, allow_inf_nan=False, strict=True)
    background_presets: list[str] = Field(default_factory=list, max_length=len(DOCUMENT_PRESETS))
    background_weights: dict[
        str, Annotated[float, Field(ge=0, le=1_000_000, allow_inf_nan=False, strict=True)]
    ] = Field(default_factory=dict, max_length=len(DOCUMENT_PRESETS))
    preset_colors: bool = Field(default=True, strict=True)
    paper_texture: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    grain: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    shadow: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    stains: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    creases: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    ruled_lines: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    scan_banding: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    speckles: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)
    binding_shadow: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def valid_appearance(self):
        validate_augmentation(self)
        return self


class JobRequest(AppearanceRequest):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    output_location: str = Field(default="default", pattern=r"^[a-z][a-z0-9-]{0,31}$")
    output_folder: str = Field(default="", max_length=512)
    language: str = "sorani"
    languages: LanguageSelection | None = None
    source: Literal["corpus", "custom", "spelling", "upload"] = "corpus"
    language_weights: dict[str, float] = Field(default_factory=dict, max_length=8)
    upload_id: str | None = Field(default=None, min_length=32, max_length=32)
    text: str = Field(default="", max_length=200_000)
    samples: int = Field(default=1000, ge=1, le=100_000, strict=True)
    font_size: int = Field(default=48, ge=12, le=128, strict=True)
    fonts: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("fonts")
    @classmethod
    def chosen_font_ids(cls, value):
        if any(not FONT_ID.fullmatch(item) for item in value):
            raise ValueError("Fonts are chosen by their ID from /api/fonts")
        return value

    format: Literal["png", "jpg", "webp", "tif"] = "png"
    seed: int = Field(default=42, ge=-(2**63), le=2**63 - 1, strict=True)
    workers: int = Field(default=1, ge=1, le=4, strict=True)
    train_ratio: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)
    validation_ratio: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def coherent(self):
        if self.name is not None and (not NAME.fullmatch(self.name) or not _folder_name(self.name)):
            raise ValueError("Dataset name may contain letters, numbers, underscores and hyphens")
        _folder_parts(self.output_folder)
        if self.language not in LANGUAGES and not LANGUAGE.fullmatch(self.language):
            raise ValueError("Choose Sorani, Kurmanji, both, all or a valid source language tag")
        tags = _language_tags(self.language, self.languages)
        if self.language_weights and (
            not set(self.language_weights) <= set(tags)
            or any(
                not isfinite(value) or not 0 <= value <= 1_000_000
                for value in self.language_weights.values()
            )
            or not any(self.language_weights.get(tag, 1) > 0 for tag in tags)
        ):
            raise ValueError(
                "Language shares must be numbers for the selected languages, "
                "with at least one above zero"
            )
        if self.train_ratio + self.validation_ratio > 1:
            raise ValueError("Training and validation proportions must add up to at most 1")
        if len(self.text.encode("utf-8")) > 200 * 1024:
            raise ValueError("Custom text must be at most 200 KiB")
        if self.source == "custom":
            if len(tags) != 1:
                raise ValueError("Choose one explicit source language for your own text")
            if not required_codepoints(self.text):
                raise ValueError("Enter at least one line containing visible text or numbers")
        elif self.text:
            raise ValueError("Custom text is only accepted with the custom text source")
        if self.source == "spelling" and (
            not tags or any(tag not in {"ckb", "kmr"} for tag in tags)
        ):
            raise ValueError("Spelling checks support Sorani, Kurmanji or both")
        if self.source == "upload":
            if self.upload_id is None or not JOB_ID.fullmatch(self.upload_id):
                raise ValueError("Upload a text file before generating")
        elif self.upload_id is not None:
            raise ValueError("Uploaded files are only accepted with the upload source")
        return self


class PreviewRequest(AppearanceRequest):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=200_000)
    language: str = "sorani"
    languages: LanguageSelection | None = None
    source: Literal["demo", "corpus", "spelling"] = "demo"
    index: int = Field(default=0, ge=0, le=31, strict=True)
    font_size: int = Field(default=48, ge=12, le=128, strict=True)
    augmentation: float = Field(default=0, ge=0, le=1, allow_inf_nan=False, strict=True)
    seed: int = Field(default=42, ge=-(2**63), le=2**63 - 1, strict=True)
    compare: bool = Field(default=False, strict=True)
    fonts: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator("fonts")
    @classmethod
    def chosen_font_ids(cls, value):
        if any(not FONT_ID.fullmatch(item) for item in value):
            raise ValueError("Fonts are chosen by their ID from /api/fonts")
        return value

    @model_validator(mode="after")
    def bounded_text(self):
        if len(self.text.encode("utf-8")) > 200 * 1024:
            raise ValueError("Preview text must be at most 200 KiB")
        tags = _language_tags(self.language, self.languages)
        if self.languages is not None and self.source == "demo" and self.text and len(tags) != 1:
            raise ValueError("Choose one explicit source language for your preview text")
        if self.source == "spelling" and (
            not tags or any(tag not in {"ckb", "kmr"} for tag in tags)
        ):
            raise ValueError("Spelling examples support Sorani, Kurmanji or both")
        return self


class CapacityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=200_000)
    language: str = "sorani"
    languages: LanguageSelection | None = None

    @model_validator(mode="after")
    def coherent(self):
        if len(self.text.encode("utf-8")) > 200 * 1024:
            raise ValueError("Custom text must be at most 200 KiB")
        if len(_language_tags(self.language, self.languages)) != 1:
            raise ValueError("Choose one explicit source language for your own text")
        return self


def _now():
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: dict):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, encoding="utf-8", delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(value, file, ensure_ascii=False)
            file.write("\n")
        # Close before renaming: Windows cannot rename a file that is still open.
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _language_tags(language, languages=None):
    if languages is not None:
        if (
            not isinstance(languages, list)
            or not 1 <= len(languages) <= 4
            or any(tag not in FOCUSED_LANGUAGES for tag in languages)
            or len(set(languages)) != len(languages)
        ):
            raise ValueError(
                "Choose distinct source languages from Sorani, Kurmanji, Arabic and English"
            )
        return list(languages)
    if not isinstance(language, str) or (
        language not in LANGUAGES and not LANGUAGE.fullmatch(language)
    ):
        raise ValueError("Choose a valid source language")
    return [canonical_language(tag) for tag in LANGUAGES.get(language, [language])]


def _custom_source(raw: Path, text: str, language: str) -> Path:
    raw.mkdir()
    path = raw / "custom.jsonl"
    with path.open("x", encoding="utf-8") as stream:
        for line, value in enumerate(io.StringIO(text), 1):
            value = value.removesuffix("\n").removesuffix("\r")
            if not value.strip():
                continue
            stream.write(
                json.dumps(
                    {
                        "text": value,
                        "language": language,
                        "genre": "user-text",
                        "attribution": {
                            "source": "User supplied text",
                            "line": line,
                            "modified": False,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return path


def _lock_output(metadata: Path):
    """Only one studio may recover and write job history for this output root."""
    path = metadata / ".server.lock"
    if path.is_symlink():
        raise ValueError("Studio lock must not be a symbolic link")
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        stream.close()
        raise ValueError(
            "Another SynthOCR studio is using this output folder. "
            "Use its browser tab, stop that server, or choose another --output-root."
        ) from error
    return stream


def _archive_size(source: Path):
    """Size of a stored ZIP64, without a dataset-sized entry list."""
    size = 98  # ZIP64 end record, locator and classic end record.
    for path in dataset_files(source):
        if path.is_symlink():
            raise ValueError("Dataset archives cannot contain symbolic links")
        if path.is_file():
            size += (
                path.stat().st_size + 124 + 2 * len(path.relative_to(source).as_posix().encode())
            )
    return size


def _stored_zip(source: Path, destination: Path):
    """Write ZIP64 without ZipFile's in-memory list of every image entry.

    PNG/JPEG/WebP/TIFF images are already compressed. Stored entries also avoid
    spending CPU recompressing them; the central directory is spooled to disk.
    """
    with destination.open("xb") as output, tempfile.TemporaryFile() as directory:
        count = 0
        for path in dataset_files(source):
            if path.is_symlink():
                raise ValueError("Dataset archives cannot contain symbolic links")
            if not path.is_file():
                continue
            name = path.relative_to(source).as_posix().encode("utf-8")
            size, crc = 0, 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    crc = zlib.crc32(chunk, crc)
            offset = output.tell()
            extra = struct.pack("<HHQQ", 1, 16, size, size)
            output.write(
                struct.pack(
                    "<IHHHHHIIIHH",
                    0x04034B50,
                    45,
                    0x800,
                    0,
                    0,
                    33,
                    crc,
                    0xFFFFFFFF,
                    0xFFFFFFFF,
                    len(name),
                    len(extra),
                )
                + name
                + extra
            )
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, output, 1024 * 1024)
            extra = struct.pack("<HHQQQ", 1, 24, size, size, offset)
            directory.write(
                struct.pack(
                    "<IHHHHHHIIIHHHHHII",
                    0x02014B50,
                    45,
                    45,
                    0x800,
                    0,
                    0,
                    33,
                    crc,
                    0xFFFFFFFF,
                    0xFFFFFFFF,
                    len(name),
                    len(extra),
                    0,
                    0,
                    0,
                    0,
                    0xFFFFFFFF,
                )
                + name
                + extra
            )
            count += 1
        central_offset, central_size = output.tell(), directory.tell()
        directory.seek(0)
        shutil.copyfileobj(directory, output, 1024 * 1024)
        zip64_offset = output.tell()
        output.write(
            struct.pack(
                "<IQHHIIQQQQ",
                0x06064B50,
                44,
                45,
                45,
                0,
                0,
                count,
                count,
                central_size,
                central_offset,
            )
        )
        output.write(struct.pack("<IIQI", 0x07064B50, 0, zip64_offset, 1))
        output.write(
            struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
        )


class _ArchiveResponse(FileResponse):
    def __init__(self, path, *, release, **kwargs):
        super().__init__(path, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also release pins when a browser disconnects partway through a ZIP.
            with CancelScope(shield=True):
                await run_in_threadpool(self.release, self.path)


class Studio:
    def __init__(
        self,
        config: Config,
        output_root: Path | None = None,
        *,
        cache_mb: int = 256,
        output_locations: dict[str, Path] | None = None,
    ):
        if isinstance(cache_mb, bool) or not isinstance(cache_mb, int) or cache_mb < 1:
            raise ValueError("Studio cache size must be a positive number of MiB")
        self.cache_limit = cache_mb * 1024 * 1024
        self.config = config.validate()
        self.output_root = (output_root or config.path("dataset/ui")).resolve()
        self.protected = (
            config.path(config.data.raw),
            config.path(config.data.processed),
            *(config.path(path) for path in config.fonts.directories),
        )
        if output_locations is not None and (
            not isinstance(output_locations, dict)
            or len(output_locations) > 8
            or any(
                not isinstance(name, str) or not LOCATION.fullmatch(name) or name == "default"
                for name in output_locations
            )
            or any(
                not isinstance(path, (str, os.PathLike)) or not str(path).strip()
                for path in output_locations.values()
            )
        ):
            raise ValueError("Use at most eight named output locations; 'default' is reserved")
        self.output_locations = {
            "default": self.output_root,
            **{name: config.path(str(path)) for name, path in (output_locations or {}).items()},
        }
        roots = list(self.output_locations.values())
        for index, root in enumerate(roots):
            self._validate_output_root(root, roots[:index])
        self.metadata = self.output_root / ".jobs"
        self.cache = self.metadata / ".cache"
        self.uploads = self.metadata / "uploads"
        # Fonts added in the app stay in this workspace, never in the project's fonts/.
        self.user_fonts = self.metadata / "fonts"
        self._root_locks = []
        try:
            for root in roots:
                root.mkdir(parents=True, exist_ok=True)
                metadata = root / ".jobs"
                if metadata.is_symlink():
                    raise ValueError("Job metadata directory must not be a symbolic link")
                metadata.mkdir(exist_ok=True)
                self._root_locks.append(_lock_output(metadata))
            for path, label in (
                (self.cache, "Studio cache"),
                (self.uploads, "Upload directory"),
                (self.user_fonts, "User font directory"),
            ):
                if path.is_symlink():
                    raise ValueError(f"{label} must not be a symbolic link")
                path.mkdir(exist_ok=True)
        except BaseException:
            for lock in reversed(self._root_locks):
                lock.close()
            raise
        self.lock = threading.RLock()
        self.upload_lock = threading.RLock()
        self.upload_validation_lock = threading.Lock()
        self.archive_lock = threading.RLock()
        self.preview_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="synthocr-studio")
        self.active: dict | None = None
        self.cancel_event = threading.Event()
        self._fonts = None
        self._font_catalog = None
        self._coverage = Coverage(min(config.fonts.coverage_cache_size, 128))
        self._capacities = OrderedDict()
        self._leased: dict[Path, int] = {}
        self._uploading: set[str] = set()
        self._claimed_uploads: set[str] = set()
        self._used_uploads = OrderedDict()
        try:
            self._trim_uploads()
            self._discard_legacy_cache()
            self._trim_cache()
            # Recover at most the displayed history, without loading a dataset-size index.
            for path in self._recent_paths():
                try:
                    job = self._load(path)
                    self.job_root(job)
                    if (
                        isinstance(job.get("upload"), dict)
                        and (self.dataset(job) / "source-upload.json").is_file()
                    ):
                        self._used_uploads[job["upload"]["id"]] = job["name"]
                except OSError, ValueError, HTTPException:
                    continue
                if job.get("status") in ACTIVE:
                    job.update(
                        status="failed",
                        error="The app closed before generation finished. Start a new run.",
                    )
                    self._save(job)
        except BaseException:
            self.close()
            raise

    def _validate_output_root(self, root: Path, others):
        if any(
            path.name.casefold() == ".jobs" or self._is_dataset(path)
            for path in (root, *root.parents)
        ):
            raise ValueError("Choose an output root outside existing datasets and job metadata")
        if any(self._roots_overlap(root, path) for path in self.protected):
            raise ValueError("UI output root must be separate from raw, processed data and fonts")
        if any(self._roots_overlap(root, other) for other in others):
            raise ValueError("Output locations must not overlap or refer to the same folder")

    @staticmethod
    def _roots_overlap(first: Path, second: Path) -> bool:
        if first.is_relative_to(second) or second.is_relative_to(first):
            return True
        # Case-insensitive volumes may expose the same folder through differently
        # cased POSIX paths. Compare existing ancestors by filesystem identity.
        for root, other in ((first, second), (second, first)):
            if not root.exists():
                continue
            for candidate in (other, *other.parents):
                if candidate.exists() and root.samefile(candidate):
                    return True
        return False

    def _discard_legacy_cache(self):
        # Earlier studio versions retained a second export tree and ZIP for every
        # format. These directories contain only disposable studio derivatives.
        with os.scandir(self.metadata) as entries:
            for entry in entries:
                if JOB_ID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                elif entry.name.startswith(".export-") and entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
        with os.scandir(self.cache) as entries:
            for entry in entries:
                if entry.name.endswith(".tmp") and entry.is_file(follow_symlinks=False):
                    Path(entry.path).unlink(missing_ok=True)

    def _cache_files(self):
        if self.cache.is_symlink():
            raise HTTPException(400, "Invalid studio cache")
        with os.scandir(self.cache) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)

    def _trim_cache(self, reserve=0):
        """Evict only disposable derivatives; never remove original datasets."""
        if reserve > self.cache_limit:
            raise HTTPException(
                413,
                "This download exceeds the studio cache limit. Download the manifest, "
                "use the dataset in the output folder, or restart with a larger --cache-mb.",
            )
        while True:
            total, count, oldest = 0, 0, None
            for path in self._cache_files():
                info = path.stat()
                total += info.st_size
                count += 1
                if path not in self._leased and (oldest is None or info.st_mtime_ns < oldest[0]):
                    oldest = (info.st_mtime_ns, path)
            if total + reserve <= self.cache_limit and count < CACHE_FILES:
                return total
            if oldest is None:
                raise HTTPException(
                    503, "Download cache is busy; retry after current downloads finish"
                )
            oldest[1].unlink(missing_ok=True)

    def storage(self):
        with self.archive_lock:
            return {
                "cache_limit_bytes": self.cache_limit,
                "current_cache_bytes": sum(path.stat().st_size for path in self._cache_files()),
                "policy": "Least recently used downloads are removed; datasets are retained",
                "upload_limit_bytes": UPLOAD_BYTES,
            }

    def location_root(self, location: str, expected: str | None = None) -> Path:
        if not isinstance(location, str) or location not in self.output_locations:
            raise HTTPException(404, "Output location is not available")
        root = self.output_locations[location]
        if expected is not None and expected != str(root):
            raise HTTPException(
                409, "This dataset's output location has changed; restore its original location"
            )
        if root.is_symlink() or root.resolve() != root or not root.is_dir():
            raise HTTPException(400, "Output location is not an accessible ordinary directory")
        return root

    def job_root(self, job) -> Path:
        location = job.get("output_location", "default")
        expected = job.get("output_root")
        if location != "default" and not isinstance(expected, str):
            raise HTTPException(400, "Dataset output location identity is missing")
        return self.location_root(location, expected)

    @staticmethod
    def _is_dataset(path: Path) -> bool:
        return any(
            (path / name).exists() or (path / name).is_symlink()
            for name in ("run.json", "manifest.jsonl")
        )

    def directory(self, location: str, path: str = "", expected: str | None = None) -> Path:
        root = self.location_root(location, expected)
        parts = _folder_parts(path)
        # Generation creates the image directory before writing run/manifest
        # markers. Reserve the entire active destination during that interval,
        # including queued output that does not yet exist. Derive its path from
        # recorded components; dataset() would recurse through directory().
        reserved = None
        active = self.active
        if active and active["status"] in ACTIVE:
            active_root = self.output_locations.get(active.get("output_location", "default"))
            name = active.get("name")
            if (
                active_root
                and isinstance(name, str)
                and NAME.fullmatch(name)
                and _folder_name(name)
            ):
                reserved = active_root.joinpath(
                    *_folder_parts(active.get("output_folder", "")), name
                )
        current = root
        for index in range(len(parts) + 1):
            if reserved is not None and current.is_relative_to(reserved):
                raise HTTPException(409, "That folder is reserved for the active dataset")
            if current.is_symlink() or current.resolve() != current or not current.is_dir():
                raise HTTPException(400, "Choose an existing folder without symbolic links")
            if self._is_dataset(current):
                raise HTTPException(400, "Choose a folder outside existing datasets")
            if any(current.is_relative_to(protected) for protected in self.protected):
                raise HTTPException(
                    400, "Output folders must stay separate from source data and fonts"
                )
            if index < len(parts):
                current = current / parts[index]
        return current

    def directories(self, location: str = "default", path: str = ""):
        parent = self.directory(location, path)
        children, truncated = [], False
        with os.scandir(parent) as entries:
            for scanned, entry in enumerate(entries):
                if scanned >= DIRECTORY_SCAN_LIMIT:
                    truncated = True
                    break
                if not _folder_name(entry.name) or not entry.is_dir(follow_symlinks=False):
                    continue
                relative = "/".join(filter(None, (path, entry.name)))
                try:
                    self.directory(location, relative)
                except OSError, ValueError, HTTPException:
                    continue
                if len(children) >= DIRECTORY_LIMIT:
                    truncated = True
                    break
                children.append({"name": entry.name, "path": relative})
        children.sort(key=lambda entry: entry["name"].casefold())
        return {
            "location": location,
            "path": path,
            "parent": path.rpartition("/")[0] if path else None,
            "directories": children,
            "truncated": truncated,
            "root": str(self.output_locations[location]),
            "absolute_path": str(parent),
        }

    def create_directory(self, request: DirectoryRequest):
        with self.lock:
            relative = "/".join(filter(None, (request.path, request.name)))
            _folder_parts(relative)
            parent = self.directory(request.location, request.path)
            target = parent / request.name
            if target.exists() or target.is_symlink():
                raise HTTPException(
                    409, "That folder already exists; choose it or use another name"
                )
            if (
                self.active
                and self.active["status"] in ACTIVE
                and target == self.dataset(self.active)
            ):
                raise HTTPException(409, "That folder is reserved for the active dataset")
            target.mkdir()
            return self.directories(request.location, relative)

    def _pending_uploads(self):
        if self.uploads.is_symlink():
            raise HTTPException(400, "Invalid upload directory")
        with os.scandir(self.uploads) as entries:
            for entry in entries:
                if JOB_ID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                    yield Path(entry.path)

    def _trim_uploads(self):
        """Expire unused inputs only; retained dataset sources are never removed."""
        with self.upload_lock:
            now = time.time()
            for path in self._pending_uploads():
                if path.name in self._uploading | self._claimed_uploads:
                    continue
                if not (path / "upload.json").is_file() or now - path.stat().st_mtime > UPLOAD_TTL:
                    shutil.rmtree(path)

    def _upload_usage(self):
        size, count = 0, 0
        for path in self._pending_uploads():
            count += 1
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.startswith("source.") and entry.is_file(follow_symlinks=False):
                        size += entry.stat(follow_symlinks=False).st_size
        return size, count

    def _begin_upload(self, filename, language):
        if (
            not filename
            or len(filename.encode("utf-8")) > 255
            or Path(filename).name != filename
            or "\\" in filename
            or any(ord(char) < 32 for char in filename)
        ):
            raise HTTPException(422, "Choose a TXT, JSONL or CSV file")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".txt", ".jsonl", ".csv"}:
            raise HTTPException(422, "Upload a UTF-8 TXT, JSONL or CSV file")
        if len(language) > 128 or (language not in LANGUAGES and not LANGUAGE.fullmatch(language)):
            raise HTTPException(422, "Choose a source language for the uploaded file")
        with self.upload_lock:
            self._trim_uploads()
            size, count = self._upload_usage()
            if count >= UPLOAD_FILES or size >= UPLOAD_TOTAL_BYTES:
                raise HTTPException(
                    413, "Pending uploads are full. Remove an unused upload or generate its dataset"
                )
            identifier = uuid4().hex
            path = self.uploads / identifier
            path.mkdir()
            self._uploading.add(identifier)
            return identifier, path / f"source{suffix}"

    def _write_upload_chunk(self, stream, chunk):
        with self.upload_lock:
            if stream.tell() + len(chunk) > UPLOAD_BYTES:
                raise HTTPException(413, "Uploaded files must be at most 50 MiB")
            size, _ = self._upload_usage()
            if size + len(chunk) > UPLOAD_TOTAL_BYTES:
                raise HTTPException(413, "Pending uploads exceed the 100 MiB storage limit")
            stream.write(chunk)
            # Make accepted bytes visible to quota checks for simultaneous uploads.
            stream.flush()

    def _validate_upload(self, identifier, path, filename, language, selected_languages=None):
        with (
            self.upload_validation_lock,
            tempfile.TemporaryDirectory(prefix="synthocr-upload-index-") as directory,
        ):
            db = sqlite3.connect(Path(directory) / "labels.sqlite3")
            try:
                db.execute("PRAGMA cache_size=-2048")
                db.execute("PRAGMA temp_store=FILE")
                db.execute("PRAGMA journal_mode=OFF")
                db.execute(
                    "CREATE TABLE labels (id INTEGER PRIMARY KEY, language TEXT, text TEXT, "
                    "genre TEXT, UNIQUE(language,text))"
                )
                return self._inspect_upload(
                    identifier, path, filename, language, db, selected_languages
                )
            finally:
                db.close()

    def _inspect_upload(self, identifier, path, filename, language, db, selected_languages=None):
        selection_languages = _language_tags(language, selected_languages)
        selected = set(selection_languages)
        excluded = {*self.config.generation.exclude_genres, "spelling"}
        fallback = selection_languages
        fallback = fallback[0] if len(fallback) == 1 else ""
        count, unlabelled, languages = 0, 0, set()
        for line, row in records(path, path.parent, self.config.data.max_line_chars):
            value = row.get("text")
            if not isinstance(value, str):
                raise ValueError(f"Line {line}: text must be a string")
            value.encode("utf-8", errors="strict")
            text = normalize(value, "NFC")
            if not required_codepoints(text):
                continue
            tag = row.get("language") or ""
            if not isinstance(tag, str) or len(tag) > 128:
                raise ValueError(f"Line {line}: language must be a language tag")
            if not isinstance(row.get("attribution", {}), dict):
                raise ValueError(f"Line {line}: attribution must be an object")
            genre = row.get("genre") or path.stem
            if not isinstance(genre, str):
                raise ValueError(f"Line {line}: genre must be a string")
            if not tag:
                unlabelled += 1
                if not fallback:
                    raise HTTPException(
                        422,
                        "This file has text without language tags. Choose one explicit language, "
                        "or add a language field to every JSONL/CSV record",
                    )
                tag = fallback
            tag = resolve_source_language(tag, row.get("attribution", {}))
            tag = canonical_language(tag)
            languages.add(tag)
            if len(languages) > 128:
                raise HTTPException(422, "An uploaded file may contain at most 128 language tags")
            count += 1
            for label in chunks(text, self.config.data.max_chars):
                if required_codepoints(label):
                    db.execute(
                        "INSERT OR IGNORE INTO labels(language,text,genre) VALUES(?,?,?)",
                        (tag, label, genre),
                    )
                    # Shared preparation keeps the first origin, except that
                    # natural text replaces an identical spelling diagnostic.
                    if genre != "spelling":
                        db.execute(
                            "UPDATE labels SET genre=? WHERE language=? AND text=? "
                            "AND genre='spelling'",
                            (genre, tag, label),
                        )
        if not count:
            raise HTTPException(422, "The file contains no visible text or numbers")
        conditions, parameters = [], []
        if selected:
            conditions.append(f"language IN ({','.join('?' for _ in selected)})")
            parameters.extend(sorted(selected))
        if excluded:
            conditions.append(f"genre NOT IN ({','.join('?' for _ in excluded)})")
            parameters.extend(sorted(excluded))
        selection = " WHERE " + " AND ".join(conditions) if conditions else ""
        selected_records = db.execute(
            "SELECT COUNT(*) FROM labels" + selection, parameters
        ).fetchone()[0]
        if not selected_records:
            raise HTTPException(
                422,
                "The uploaded file has no usable text for the selected language. "
                "Check its language tags and excluded genres",
            )
        preview_language, first_label = db.execute(
            "SELECT language,text FROM labels" + selection + " ORDER BY id LIMIT 1", parameters
        ).fetchone()
        preview = next(chunks(first_label, min(self.config.data.max_chars, 200)), "")
        document = {
            "id": identifier,
            "filename": filename,
            "stored_filename": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "records": selected_records,
            "source_records": count,
            "languages": sorted(languages),
            "selected_languages": selection_languages,
            "unlabelled_records": unlabelled,
            "language": language,
            "preview_text": preview,
            "preview_language": preview_language,
            "created_at": _now(),
        }
        _atomic_json(path.parent / "upload.json", document)
        return document

    async def upload(self, request, filename, language, languages=None):
        try:
            _language_tags(language, languages)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        identifier, path = await run_in_threadpool(self._begin_upload, filename, language)
        successful = False
        try:
            with path.open("xb", buffering=0) as stream:
                async for chunk in request.stream():
                    # ASGI controls incoming chunk size; disk writes and quota checks
                    # stay bounded even when a client sends the entire body at once.
                    view = memoryview(chunk)
                    for start in range(0, len(view), UPLOAD_CHUNK):
                        await run_in_threadpool(
                            self._write_upload_chunk, stream, view[start : start + UPLOAD_CHUNK]
                        )
            arguments = (identifier, path, filename, language)
            result = await run_in_threadpool(
                self._validate_upload, *arguments, *([languages] if languages is not None else [])
            )
            if await request.is_disconnected():
                raise HTTPException(499, "Upload cancelled")
            successful = True
            return result
        finally:
            with self.upload_lock:
                self._uploading.discard(identifier)
                if not successful:
                    shutil.rmtree(path.parent, ignore_errors=True)

    def uploaded(self, identifier):
        if not JOB_ID.fullmatch(identifier):
            raise HTTPException(404, "Upload not found; choose the file again")
        with self.upload_lock:
            if identifier in self._used_uploads:
                raise HTTPException(
                    409,
                    f"This file was saved with dataset {self._used_uploads[identifier]}. "
                    "Choose the file again to generate another dataset",
                )
            self._trim_uploads()
            directory = self.uploads / identifier
            metadata = directory / "upload.json"
            try:
                if (
                    directory.is_symlink()
                    or metadata.is_symlink()
                    or metadata.stat().st_size > 65536
                ):
                    raise ValueError("Invalid upload metadata")
                upload = json.loads(metadata.read_text(encoding="utf-8"))
                filename = upload["stored_filename"]
                if filename not in {"source.txt", "source.csv", "source.jsonl"}:
                    raise ValueError("Invalid upload file")
                path = directory / filename
                if path.is_symlink() or not path.is_file() or upload["id"] != identifier:
                    raise ValueError("Invalid upload source")
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise HTTPException(404, "Upload not found; choose the file again") from error
            return upload, path

    def remove_upload(self, identifier):
        with self.upload_lock:
            self.uploaded(identifier)
            if identifier in self._claimed_uploads:
                raise HTTPException(409, "This file is being used by the current generation")
            shutil.rmtree(self.uploads / identifier)
            return {"removed": True}

    def _release_archive(self, path):
        with self.archive_lock:
            self._leased[path] -= 1
            if not self._leased[path]:
                del self._leased[path]

    def _recent_paths(self):
        def candidates():
            with os.scandir(self.metadata) as entries:
                for entry in entries:
                    if (
                        entry.name.endswith(".json")
                        and JOB_ID.fullmatch(entry.name[:-5])
                        and entry.is_file(follow_symlinks=False)
                    ):
                        yield Path(entry.path)

        return heapq.nlargest(
            20,
            candidates(),
            key=lambda p: p.stat().st_mtime_ns,
        )

    def _load(self, path):
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            raise ValueError("Invalid job metadata")
        job = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(job, dict)
            or job.get("id") != path.stem
            or not isinstance(job.get("name"), str)
            or not NAME.fullmatch(job["name"])
            or not isinstance(job.get("status"), str)
            or job.get("status") not in ACTIVE | {"completed", "cancelled", "failed"}
        ):
            raise ValueError("Invalid job metadata")
        return job

    def _save(self, job):
        job["updated_at"] = _now()
        _atomic_json(self.metadata / f"{job['id']}.json", job)

    def job(self, identifier):
        if not JOB_ID.fullmatch(identifier):
            raise HTTPException(404, "Job not found")
        with self.lock:
            if self.active and self.active["id"] == identifier:
                return self.active.copy()
            path = self.metadata / f"{identifier}.json"
            try:
                job = self._load(path)
            except (OSError, ValueError, KeyError) as error:
                raise HTTPException(404, "Job not found") from error
            if job.get("id") != identifier or not NAME.fullmatch(job.get("name", "")):
                raise HTTPException(404, "Job not found")
            self.job_root(job)
            if job.get("status") in ACTIVE:
                job.update(
                    status="failed",
                    error="The app closed before generation finished. Start a new run.",
                )
                self._save(job)
            return job

    def jobs(self):
        with self.lock:
            result = []
            for path in self._recent_paths():
                try:
                    result.append(self.job(path.stem))
                except HTTPException:
                    continue
            return result

    def dataset(self, job):
        root = self.job_root(job)
        parent = self.directory(
            job.get("output_location", "default"), job.get("output_folder", ""), str(root)
        )
        if not NAME.fullmatch(job["name"]) or not _folder_name(job["name"]):
            raise HTTPException(400, "Invalid dataset name")
        path = parent / job["name"]
        if path.is_symlink() or path.resolve().parent != parent:
            raise HTTPException(400, "Dataset path must stay inside the configured output root")
        if any(
            (path / name).is_symlink()
            for name in (
                "manifest.jsonl",
                "origins.jsonl",
                "run.json",
                "stats.json",
                "images",
                "source.jsonl",
                "source.txt",
                "source.csv",
                "source-upload.json",
            )
        ):
            raise HTTPException(400, "Dataset artifacts must not be symbolic links")
        return path

    def font_directories(self):
        return [*(self.config.path(p) for p in self.config.fonts.directories), self.user_fonts]

    def fonts(self):
        with self.lock:
            if self._fonts is None:
                self._fonts = discover(self.font_directories())
            return self._fonts

    def font_catalog(self):
        """Every usable face with a readable name and the page languages it draws."""
        with self.lock:
            if self._font_catalog is None:
                fonts = self.fonts()
                ids = font_ids(fonts)
                user = self.user_fonts.resolve()
                entries = [
                    {
                        "id": ids[font],
                        "name": _font_display_name(font),
                        "file": Path(font.path).name,
                        "user": Path(font.path).parent == user,
                        "languages": [
                            tag
                            for tag, sample in LANGUAGE_SAMPLES.items()
                            if not self._coverage.missing(font, required_codepoints(sample))
                        ],
                    }
                    for font in fonts
                ]
                entries.sort(key=lambda entry: (entry["name"].casefold(), entry["id"]))
                self._font_catalog = (entries, ids)
            return self._font_catalog

    def selected_fonts(self, include):
        fonts = self.fonts()
        if not include:
            return fonts
        try:
            return select(fonts, include, self.font_catalog()[1])
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    def _forget_fonts(self):
        self._fonts = None
        self._font_catalog = None

    async def add_font(self, request, filename):
        suffix = Path(filename).suffix.lower()
        if suffix not in FONT_SUFFIXES or not _folder_name(Path(filename).name):
            raise HTTPException(422, "Choose a TTF, OTF, TTC or OTC font file")
        # Not a font suffix, so discovery never sees a partial upload.
        temporary = self.user_fonts / f".upload-{uuid4().hex}.part"
        try:
            size = 0
            with temporary.open("xb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > FONT_UPLOAD_BYTES:
                        raise HTTPException(413, "Font files must be at most 32 MiB")
                    await run_in_threadpool(stream.write, chunk)
            return await run_in_threadpool(self._install_font, temporary, filename, suffix)
        finally:
            temporary.unlink(missing_ok=True)

    def _install_font(self, temporary, filename, suffix):
        if not temporary.stat().st_size:
            raise HTTPException(422, "The font file is empty")
        try:
            faces = 1
            if suffix in {".ttc", ".otc"}:
                with TTCollection(temporary, lazy=True) as collection:
                    faces = len(collection.fonts)
            for index in range(faces):
                ImageFont.truetype(str(temporary), 12, index=index)
        except (OSError, ValueError, TTLibError) as error:
            raise HTTPException(422, "This file is not a usable font") from error
        digest = sha256(temporary)
        with self.lock:
            entries, _ = self.font_catalog()
            existing = [entry for entry in entries if entry["id"].startswith(f"{digest}:")]
            if existing:
                return {"fonts": existing, "added": False}
            if sum(1 for _ in self.user_fonts.glob("*")) >= USER_FONT_LIMIT:
                raise HTTPException(409, f"At most {USER_FONT_LIMIT} fonts can be added")
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).stem).strip("-.")[:60]
            os.replace(temporary, self.user_fonts / f"{stem or 'font'}-{digest[:12]}{suffix}")
            self._forget_fonts()
            entries, _ = self.font_catalog()
            added = [entry for entry in entries if entry["id"].startswith(f"{digest}:")]
            return {"fonts": added, "added": True}

    def remove_font(self, digest):
        with self.lock:
            if self.active and self.active["status"] in ACTIVE:
                raise HTTPException(409, "Wait for the current generation to finish")
            entries, _ = self.font_catalog()
            match = next(
                (e for e in entries if e["user"] and e["id"].startswith(f"{digest}:")), None
            )
            if match is None:
                raise HTTPException(404, "Only fonts you added can be removed")
            path = self.user_fonts / match["file"]
            if path.is_symlink() or path.parent.resolve() != self.user_fonts.resolve():
                raise HTTPException(400, "Invalid font file")
            path.unlink()
            self._forget_fonts()
            return {"removed": digest}

    def start(self, request: JobRequest):
        with self.lock:
            if self.active and self.active["status"] in ACTIVE:
                raise HTTPException(
                    409, "A generation is already running; cancel or finish it first"
                )
        self.directory(request.output_location, request.output_folder)
        self.selected_fonts(request.fonts)
        if request.source == "custom":
            capacity = self.capacity(
                CapacityRequest(
                    text=request.text, language=request.language, languages=request.languages
                )
            )
            if capacity["unsupported_texts"]:
                raise HTTPException(
                    422,
                    {
                        "message": (
                            "Installed fonts cannot render every custom line; "
                            "edit the text or add compatible fonts"
                        ),
                        **capacity,
                    },
                )
            if request.samples > capacity["available_pairs"]:
                raise HTTPException(
                    422,
                    {
                        "message": (
                            f"Your {capacity['texts']} distinct lines support "
                            f"{capacity['available_pairs']} unique text/font pairs. "
                            "Request fewer images or add more text"
                        ),
                        **capacity,
                    },
                )
        tags = _language_tags(request.language, request.languages)
        required = (
            {row["attribution"]["source_id"] for row in spelling_cases() if row["language"] in tags}
            if request.source == "spelling"
            else set()
        )
        if request.samples < len(required):
            raise HTTPException(422, f"This spelling check needs at least {len(required)} images")
        with self.lock:
            if self.active and self.active["status"] in ACTIVE:
                raise HTTPException(
                    409, "A generation is already running; finish or cancel it first"
                )
            identifier = uuid4().hex
            name = request.name or f"{request.language}-{identifier[:8]}"
            job = {
                "id": identifier,
                "name": name,
                "language": request.language,
                "languages": tags,
                "source": request.source,
                "status": "queued",
                "phase": "queued",
                "generated": 0,
                "requested": request.samples,
                "progress": 0,
                "output_location": request.output_location,
                "output_folder": request.output_folder,
                "output_root": str(self.location_root(request.output_location)),
                "error": None,
                "result": None,
                "created_at": _now(),
                "updated_at": _now(),
                "settings": request.model_dump(exclude={"text"}),
            }
            destination = self.dataset(job)
            job["output"] = str(destination)
            if destination.exists():
                raise HTTPException(409, "That dataset name already exists; choose a fresh name")
            if request.source == "upload":
                with self.upload_lock:
                    upload, path = self.uploaded(request.upload_id)
                    selected = {canonical_language(tag) for tag in tags}
                    upload_selection = upload.get(
                        "selected_languages", _language_tags(upload["language"])
                    )
                    if upload["unlabelled_records"] and set(tags) != set(upload_selection):
                        raise HTTPException(
                            422,
                            "This file contains unlabelled text. Keep its selected language, "
                            "or upload it again after changing the language",
                        )
                    if selected and not selected.intersection(upload["languages"]):
                        raise HTTPException(422, "The uploaded file has no text in this language")
                    if sha256(path) != upload["sha256"]:
                        raise HTTPException(422, "The uploaded file changed; upload it again")
                    self._claimed_uploads.add(request.upload_id)
                    job["upload"] = upload
            self.active = job
            self.cancel_event = threading.Event()
            self._save(job)
            try:
                self.executor.submit(self._run, job, request, required, self.cancel_event)
            except BaseException:
                self._claimed_uploads.discard(request.upload_id)
                raise
            return job.copy()

    def cancel(self, identifier):
        with self.lock:
            job = self.job(identifier)
            if self.active and self.active["id"] == identifier and job["status"] in ACTIVE:
                self.cancel_event.set()
                self.active["status"] = "cancelling"
                self.active["phase"] = "Finishing the current operation before stopping"
                self._save(self.active)
                return self.active.copy()
            return job

    def _run(self, job, request, required, cancellation):
        last_saved = 0

        def update(progress):
            nonlocal last_saved
            with self.lock:
                job["generated"] = progress.get("generated", job["generated"])
                job["progress"] = round(100 * job["generated"] / job["requested"], 1)
                if not cancellation.is_set():
                    job["status"] = progress["stage"]
                    job["phase"] = progress["stage"]
                if time.monotonic() - last_saved >= 0.5 or progress["stage"] == "verifying":
                    self._save(job)
                    last_saved = time.monotonic()

        def stopped():
            if cancellation.is_set():
                raise GenerationCancelled("Generation cancelled; partial output has been retained")

        try:
            tags = _language_tags(request.language, request.languages)
            max_chars = self.config.data.max_chars
            if required:
                max_chars = max(
                    max_chars,
                    max(len(row["text"]) for row in spelling_cases() if row["language"] in tags),
                )
            config = replace(
                self.config,
                data=replace(self.config.data, normalization="NFC", max_chars=max_chars),
                fonts=replace(
                    self.config.fonts,
                    directories=[str(path) for path in self.font_directories()],
                    include=request.fonts,
                    coverage="strict",
                    cache_size=min(self.config.fonts.cache_size, 8),
                    coverage_cache_size=min(self.config.fonts.coverage_cache_size, 128),
                ),
                generation=replace(
                    self.config.generation,
                    output=str(self.dataset(job)),
                    samples=request.samples,
                    font_size=request.font_size,
                    format=request.format,
                    **{name: getattr(request, name) for name in AUGMENTATION_FIELDS},
                    seed=request.seed,
                    workers=request.workers,
                    train_ratio=request.train_ratio,
                    validation_ratio=request.validation_ratio,
                    languages=tags,
                    language_weights=request.language_weights,
                    language="",
                    direction="auto",
                    suite="kurdish-spelling" if required else "",
                    exclude_genres=[]
                    if required
                    else [*self.config.generation.exclude_genres, "spelling"],
                ),
            ).validate()
            stopped()
            with tempfile.TemporaryDirectory(prefix="synthocr-studio-") as directory:
                custom = None
                uploaded = None
                if request.source == "custom":
                    raw, processed = Path(directory) / "raw", Path(directory) / "processed"
                    custom = _custom_source(raw, request.text, tags[0])
                    config = replace(
                        config,
                        data=replace(
                            config.data,
                            raw=str(raw),
                            processed=str(processed),
                            source_languages={},
                            deduplicate=True,
                            write_jsonl=False,
                        ),
                    )
                elif request.source == "upload":
                    upload, source = self.uploaded(request.upload_id)
                    raw, processed = Path(directory) / "raw", Path(directory) / "processed"
                    raw.mkdir()
                    uploaded = raw / source.name
                    shutil.copyfile(source, uploaded)
                    fallback = upload.get("selected_languages", _language_tags(upload["language"]))
                    config = replace(
                        config,
                        data=replace(
                            config.data,
                            raw=str(raw),
                            processed=str(processed),
                            source_languages={uploaded.name: fallback[0]}
                            if len(fallback) == 1
                            else {},
                            deduplicate=True,
                            write_jsonl=False,
                        ),
                    )
                try:
                    result = generate(config, progress=update, cancel=cancellation.is_set)
                finally:
                    # Retain user raw data alongside partial outputs as well as completed ones.
                    if custom and self.dataset(job).is_dir():
                        shutil.copyfile(custom, self.dataset(job) / "source.jsonl")
                        (self.dataset(job) / "source.txt").write_text(
                            request.text, encoding="utf-8"
                        )
                    if uploaded and self.dataset(job).is_dir():
                        target = self.dataset(job) / uploaded.name
                        shutil.copyfile(uploaded, target)
                        _atomic_json(self.dataset(job) / "source-upload.json", upload)
                        with self.upload_lock:
                            shutil.rmtree(self.uploads / request.upload_id)
                            self._used_uploads[request.upload_id] = job["name"]
                            if len(self._used_uploads) > 20:
                                self._used_uploads.popitem(last=False)
            stopped()
            update({"stage": "verifying"})
            result["verification"] = verify(self.dataset(job))
            if required:
                missing = required.copy()
                with (self.dataset(job) / "manifest.jsonl").open(encoding="utf-8") as stream:
                    for record in map(json.loads, stream):
                        missing.discard(record["attribution"]["source_id"])
                if missing:
                    raise ValueError(f"Spelling check missed {len(missing)} cases; inspect fonts")
                result["spelling_cases_checked"] = len(required)
            stopped()
            with self.lock:
                job.update(status="completed", phase="verified", result=result, progress=100)
        except GenerationCancelled as error:
            with self.lock:
                job.update(status="cancelled", phase="cancelled", error=str(error))
        except Exception as error:
            with self.lock:
                job.update(status="failed", phase="failed", error=str(error))
        finally:
            with self.upload_lock:
                self._claimed_uploads.discard(request.upload_id)
            with self.lock:
                self._save(job)

    def close(self):
        self.cancel_event.set()
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            for lock in reversed(self._root_locks):
                lock.close()

    def capacity(self, request: CapacityRequest):
        with self.preview_lock:
            key = hashlib.sha256(
                (
                    "\0".join(_language_tags(request.language, request.languages))
                    + "\0"
                    + request.text
                ).encode("utf-8")
            ).hexdigest()
            if key in self._capacities:
                self._capacities.move_to_end(key)
                return self._capacities[key]
            result = self._capacity(request)
            self._capacities[key] = result
            if len(self._capacities) > 4:
                self._capacities.popitem(last=False)
            return result

    def _capacity(self, request: CapacityRequest):
        result = {
            "texts": 0,
            "available_pairs": 0,
            "unsupported_texts": 0,
            "unsupported_examples": [],
        }
        if not required_codepoints(request.text):
            return result
        with tempfile.TemporaryDirectory(prefix="synthocr-capacity-") as directory:
            raw, processed = Path(directory) / "raw", Path(directory) / "processed"
            _custom_source(
                raw, request.text, _language_tags(request.language, request.languages)[0]
            )
            config = replace(
                self.config,
                data=replace(
                    self.config.data,
                    raw=str(raw),
                    processed=str(processed),
                    source_languages={},
                    normalization="NFC",
                    deduplicate=True,
                ),
            )
            prepare(config)
            coverage = self._coverage
            fonts = self.fonts()
            with sqlite3.connect(
                (processed / "texts.sqlite3").as_uri() + "?mode=ro", uri=True
            ) as db:
                db.execute("PRAGMA cache_size=-4096")
                for (text,) in db.execute("SELECT text FROM texts ORDER BY id"):
                    needed = required_codepoints(text)
                    compatible = sum(not coverage.missing(font, needed) for font in fonts)
                    result["texts"] += 1
                    result["available_pairs"] += compatible
                    if compatible == 0:
                        result["unsupported_texts"] += 1
                        if len(result["unsupported_examples"]) < 3:
                            result["unsupported_examples"].append(text[:120])
            return result

    def preview(self, request: PreviewRequest):
        # Keep native font allocations bounded even when the browser types quickly.
        with self.preview_lock:
            return self._preview(request)

    def _corpus_preview(self, request):
        """Read one indexed label; never prepare or count a corpus in an HTTP request."""
        tags = _language_tags(request.language, request.languages)
        excluded = sorted({*self.config.generation.exclude_genres, "spelling"})
        path = self.config.path(self.config.data.processed) / "texts.sqlite3"
        message = "Prepare the corpus with synthocr prepare, or upload a text file"
        if not path.is_file():
            raise HTTPException(400, message)
        db = None
        try:
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            db.execute("PRAGMA cache_size=-1024")
            db.execute("PRAGMA temp_store=FILE")

            def one(selected, offset, representative=False):
                conditions, parameters = [], []
                if selected:
                    conditions.append(f"language IN ({','.join('?' for _ in selected)})")
                    parameters.extend(selected)
                conditions.append(f"genre NOT IN ({','.join('?' for _ in excluded)})")
                parameters.extend(excluded)
                if representative:
                    conditions.append("genre IN ('prose','dialogue') AND length(text)>=12")
                parameters.append(offset)
                # Use the language index when available and stop after at most
                # 32 eligible labels. Avoid sorting corpora prepared without deduplication.
                return db.execute(
                    "SELECT text,language FROM texts WHERE "
                    + " AND ".join(conditions)
                    + " LIMIT 1 OFFSET ?",
                    parameters,
                ).fetchone()

            selected = [tags[request.index % len(tags)]] if len(tags) > 1 else tags
            offset = request.index // len(tags) if len(tags) > 1 else request.index
            # Prefer readable natural text for the preview. Short labels, numbers
            # and punctuation remain available to generation without restriction.
            row = (
                one(selected, offset, True)
                or one(selected, 0, True)
                or one(selected, offset)
                or one(selected, 0)
            )
            if row is None and len(tags) > 1:
                row = one(tags, 0)
            if row is None:
                raise HTTPException(400, "No usable corpus text for this language. " + message)
            return row[0], canonical_language(row[1])
        except sqlite3.Error as error:
            raise HTTPException(400, message) from error
        finally:
            if db is not None:
                db.close()

    def _spelling_preview(self, request):
        tags = _language_tags(request.language, request.languages)
        if not tags or any(tag not in {"ckb", "kmr"} for tag in tags):
            raise HTTPException(422, "Spelling examples support Sorani, Kurmanji or both")
        tag = tags[request.index % len(tags)]
        # This is the fixed, bundled spelling catalog, rather than a user corpus.
        cases = list(spelling_cases(tag))
        case = cases[(request.index // len(tags)) % len(cases)]
        return case["text"], case["language"], case["attribution"]["source_id"]

    def _preview(self, request: PreviewRequest):
        if request.language not in LANGUAGES and not LANGUAGE.fullmatch(request.language):
            raise HTTPException(422, "Choose a valid language")
        default = {
            "sorani": "سڵاو جیهان",
            "ckb": "سڵاو جیهان",
            "kurmanji": "Silav dinya",
            "kmr": "Silav dinya",
            "both": "سڵاو · Silav",
            "all": "سڵاو Hello 123",
            "ar": "مرحبا بالعالم",
            "fa": "سلام دنیا",
            "en": "Hello world",
            "he": "שלום עולם",
            "hi": "नमस्ते दुनिया",
            "zh": "你好世界",
        }
        tags = _language_tags(request.language, request.languages)
        source_language = tags[0] if len(tags) == 1 else ""
        case_id = None
        if request.source == "corpus":
            original, source_language = self._corpus_preview(request)
        elif request.source == "spelling":
            original, source_language, case_id = self._spelling_preview(request)
        else:
            if request.languages is not None and not request.text:
                source_language = tags[request.index % len(tags)]
            original = request.text or default.get(
                source_language, default.get(request.language, "123")
            )
        first_line = next((line for line in io.StringIO(original) if required_codepoints(line)), "")
        text = next(chunks(normalize(first_line), self.config.data.max_chars), "")
        if not required_codepoints(text):
            raise HTTPException(422, "Enter visible text or numbers")
        fonts = self.selected_fonts(request.fonts)
        if request.fonts:
            # Show the user's chosen fonts in turn as they ask for more examples.
            offset = request.index % len(fonts)
        else:
            # Start with a readable Kurdish face while still using shared coverage selection.
            preferred = "NotoNaskhArabic" if source_language in {"ckb", "ar"} else "NotoSans["
            offset = next((i for i, face in enumerate(fonts) if preferred in face.name), 0)
        face = self._coverage.choose(fonts, text, offset)
        if face is None:
            raise HTTPException(422, "No installed font supports this complete line")
        settings = replace(
            self.config.generation,
            font_size=request.font_size,
            direction="auto",
            language="",
            **{name: getattr(request, name) for name in AUGMENTATION_FIELDS},
            max_width=2048,
            max_height=256,
            max_pixels=524_288,
        )

        def render(appearance):
            image, info = Renderer(appearance, cache_size=1).render(
                text, face, request.seed, source_language
            )
            try:
                with io.BytesIO() as buffer:
                    image.save(buffer, format="PNG")
                    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            finally:
                image.close()
            return {"image": f"data:image/png;base64,{encoded}", **info}

        # Hold the sample, font and seed fixed while comparing effects. Only one
        # decoded image is alive at once, bounded by the preview's pixel limit.
        before = render(replace(settings, augmentation=0)) if not request.augmentation else None
        if request.compare:
            # Hidden recipe selectors retain their existing thumbnails while
            # disabled. Do not serialize a catalog of identical clean images.
            presets = list(DOCUMENT_PRESETS) if request.augmentation else []
        else:
            presets = request.background_presets
        variants = []
        distribution = background_distribution(settings)
        for preset in presets:
            rendered = (
                render(replace(settings, background_presets=[preset], background_weights={}))
                if request.augmentation
                else before
            )
            if rendered["augmentation"]:
                rendered["augmentation"]["background_probability"] = distribution.get(preset, 0)
            variants.append(
                {
                    "preset": preset,
                    "name": DOCUMENT_PRESETS[preset]["label"],
                    "text": text,
                    "font": face.name,
                    **rendered,
                }
            )
        if variants:
            selected = next((name for name, share in distribution.items() if share > 0), presets[0])
            after = next(variant for variant in variants if variant["preset"] == selected)
        else:
            after = render(settings) if request.augmentation else before
            if not request.compare or request.augmentation:
                variants.append(
                    {
                        "preset": "",
                        "name": "Custom background",
                        "text": text,
                        "font": face.name,
                        **after,
                    }
                )
        if before is None:
            # Bounds can shrink a long rotated line. Compare it with clean ink
            # at that same effective point size rather than a larger baseline.
            font_size = min(variant["font_size"] for variant in variants)
            before = render(replace(settings, augmentation=0, font_size=font_size))
        return {
            **after,
            "text": text,
            "font": face.name,
            "wrapped": text != normalize(original),
            "source": request.source,
            "source_language": source_language,
            "case_id": case_id,
            "seed": request.seed,
            "before": {"text": text, "font": face.name, **before},
            "variants": variants,
        }

    def _sample_offset(self, job, manifest, offset):
        if job["status"] != "completed" or not offset:
            return 0, 0
        info = manifest.stat()
        fingerprint = hashlib.sha256(
            f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}".encode()
        ).hexdigest()[:16]
        index = self.cache / f"{job['id']}-{fingerprint}.index"
        with self.archive_lock:
            if index.is_symlink():
                raise HTTPException(400, "Invalid gallery index")
            if not index.exists():
                # Eight bytes per 128 records, rather than an in-memory row index.
                self._trim_cache(reserve=(100_000 // INDEX_STRIDE + 1) * 8)
                temporary = self.cache / f"{uuid4().hex}.tmp"
                try:
                    with manifest.open("rb") as stream, temporary.open("xb") as target:
                        line = 0
                        while True:
                            position = stream.tell()
                            if not stream.readline():
                                break
                            if line >= 100_000:
                                raise HTTPException(400, "Dataset exceeds the studio sample limit")
                            if line % INDEX_STRIDE == 0:
                                target.write(struct.pack("<Q", position))
                            line += 1
                    os.replace(temporary, index)
                finally:
                    temporary.unlink(missing_ok=True)
            index.touch()
            entries = index.stat().st_size // 8
            if not entries:
                return 0, 0
            block = min(offset // INDEX_STRIDE, entries - 1)
            with index.open("rb") as stream:
                stream.seek(block * 8)
                return struct.unpack("<Q", stream.read(8))[0], block * INDEX_STRIDE

    def samples(self, job, offset, limit):
        path = self.dataset(job) / "manifest.jsonl"
        rows = []
        if path.is_file() and not path.is_symlink():
            position, first = self._sample_offset(job, path, offset)
            with path.open("rb") as stream:
                stream.seek(position)
                for index, line in enumerate(stream, first):
                    if index < offset:
                        continue
                    if len(rows) >= limit:
                        break
                    try:
                        row = json.loads(line)
                    except ValueError as error:
                        if job["status"] == "completed":
                            raise HTTPException(400, "Invalid sample in dataset") from error
                        break  # A running worker may still be flushing its final line.
                    if not isinstance(row, dict) or not isinstance(row.get("image"), str):
                        raise HTTPException(400, "Invalid sample in dataset")
                    filename = Path(row["image"]).name
                    if not IMAGE.fullmatch(filename):
                        raise HTTPException(400, "Invalid image name in dataset")
                    rows.append(
                        {
                            key: row.get(key)
                            for key in ("id", "text", "font", "language", "split", "augmentation")
                        }
                        | {
                            "image": f"/api/jobs/{job['id']}/images/{filename}",
                            "thumbnail": f"/api/jobs/{job['id']}/thumbnails/{filename}",
                        }
                    )
        return {"samples": rows, "total": job["generated"], "offset": offset, "limit": limit}

    def image_path(self, job, filename):
        if not IMAGE.fullmatch(filename):
            raise HTTPException(404, "Image not found")
        dataset = self.dataset(job)
        directory = dataset / "images"
        path = directory / filename
        if (
            directory.is_symlink()
            or path.is_symlink()
            or not path.is_file()
            or path.resolve().parent != directory
        ):
            raise HTTPException(404, "Image not found")
        return path

    def thumbnail(self, job, filename):
        path = self.image_path(job, filename)
        with self.preview_lock, Image.open(path) as image:
            if image.width * image.height > 16_777_216:
                raise HTTPException(400, "Image exceeds the supported preview size")
            image.thumbnail((768, 192), Image.Resampling.LANCZOS, reducing_gap=3)
            with io.BytesIO() as buffer:
                image.save(buffer, format="PNG")
                return Response(
                    buffer.getvalue(),
                    media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"},
                )

    def download(self, job, format, *, prepare_only=False):
        if job["status"] != "completed":
            raise HTTPException(409, "Downloads are available after generation and verification")
        dataset = self.dataset(job)
        with self.archive_lock:
            verify(dataset)
            prepared = {
                "url": f"/api/jobs/{job['id']}/download?format={format}",
                "filename": f"{job['name']}-{format}.zip"
                if format != "manifest"
                else f"{job['name']}-manifest.jsonl",
            }
            if format == "manifest":
                if prepare_only:
                    return prepared
                return FileResponse(
                    dataset / "manifest.jsonl",
                    media_type="application/x-ndjson",
                    filename=f"{job['name']}-manifest.jsonl",
                )
            digests = [
                sha256(dataset / name) for name in ("manifest.jsonl", "run.json", "stats.json")
            ]
            for name in ("source.txt", "source.jsonl", "source.csv", "source-upload.json"):
                if (dataset / name).is_file():
                    digests.append(sha256(dataset / name))
            digest = hashlib.sha256("".join(digests).encode()).hexdigest()[:16]
            archive = self.cache / f"{job['id']}-{digest}-{format}.zip"
            if archive.is_symlink():
                raise HTTPException(400, "Invalid download archive")
            if not archive.exists():
                # The maintained exporter hardlinks images on this filesystem.
                # Remove its attributed view after archiving instead of keeping
                # four permanent copies of each generated dataset.
                with tempfile.TemporaryDirectory(prefix=".export-", dir=self.metadata) as directory:
                    source = dataset
                    if format != "dataset":
                        retained_sources = tuple(
                            dataset / name
                            for name in (
                                "source.txt",
                                "source.jsonl",
                                "source.csv",
                                "source-upload.json",
                            )
                            if (dataset / name).is_file()
                        )
                        retained_bytes = sum(path.stat().st_size for path in retained_sources)
                        # Retained raw inputs are part of every attributed export.
                        # Account for them before creating or copying its view.
                        self._trim_cache(reserve=retained_bytes)
                        source = Path(directory) / "view"
                        try:
                            export(
                                dataset,
                                source,
                                format,
                                max_bytes=self.cache_limit - retained_bytes,
                            )
                        except ValueError as error:
                            if str(error).startswith("Export exceeds the configured byte limit"):
                                raise HTTPException(
                                    413,
                                    "This export exceeds the studio cache limit. Download the "
                                    "manifest, use the dataset in the output folder, or restart "
                                    "with a larger --cache-mb.",
                                ) from error
                            raise
                        for path in retained_sources:
                            shutil.copyfile(path, source / path.name)
                    self._trim_cache(reserve=_archive_size(source))
                    temporary = self.cache / f"{uuid4().hex}.tmp"
                    try:
                        _stored_zip(source, temporary)
                        os.replace(temporary, archive)
                    finally:
                        temporary.unlink(missing_ok=True)
            archive.touch()
            if prepare_only:
                return prepared
            self._leased[archive] = self._leased.get(archive, 0) + 1
            return _ArchiveResponse(
                archive,
                media_type="application/zip",
                filename=f"{job['name']}-{format}.zip",
                release=self._release_archive,
            )


def create_app(
    config: Config,
    output_root: Path | None = None,
    *,
    cache_mb: int = 256,
    output_locations: dict[str, Path] | None = None,
) -> FastAPI:
    studio = Studio(config, output_root, cache_mb=cache_mb, output_locations=output_locations)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            studio.close()

    app = FastAPI(title="SynthOCR Studio", version=__version__, lifespan=lifespan)
    app.state.studio = studio
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    target = request.url
                    same_origin = (
                        parsed.scheme == target.scheme
                        and parsed.username is None
                        and parsed.password is None
                        and not parsed.query
                        and not parsed.fragment
                        and parsed.path in {"", "/"}
                        and parsed.hostname == target.hostname
                        and (parsed.port or (443 if parsed.scheme == "https" else 80))
                        == (target.port or (443 if target.scheme == "https" else 80))
                    )
                except ValueError:
                    same_origin = False
                if not same_origin:
                    return JSONResponse({"detail": "Use the studio's own local browser page"}, 403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site requests are not allowed"}, 403)
            length = request.headers.get("content-length")
            upload = request.method == "POST" and request.url.path == "/api/uploads"
            remove_upload = request.method == "DELETE" and re.fullmatch(
                r"/api/(?:uploads/[a-f0-9]{32}|fonts/[a-f0-9]{64})", request.url.path
            )
            if request.method == "POST" and request.url.path == "/api/fonts":
                if length is not None and (not length.isdigit() or int(length) > FONT_UPLOAD_BYTES):
                    return JSONResponse({"detail": "Font files must be at most 32 MiB"}, 413)
            elif upload:
                if length is not None and (not length.isdigit() or int(length) > UPLOAD_BYTES):
                    return JSONResponse({"detail": "Uploaded files must be at most 50 MiB"}, 413)
            elif remove_upload:
                if length not in {None, "0"}:
                    return JSONResponse({"detail": "Removal does not accept a body"}, 413)
            elif length is None or not length.isdigit() or int(length) > 256 * 1024:
                return JSONResponse({"detail": "Request body must be at most 256 KiB"}, 413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(ValueError)
    async def invalid_operation(request, error):
        return JSONResponse({"detail": str(error)}, 400)

    @app.exception_handler(OSError)
    async def inaccessible_output(request, error):
        return JSONResponse({"detail": f"Cannot access output data: {error}"}, 400)

    @app.get("/api/info")
    def info():
        corpora = []
        languages = [
            {"id": key, "name": name}
            for key, name in (
                ("sorani", "Sorani · سۆرانی"),
                ("kurmanji", "Kurmanji · Kurmancî"),
                ("both", "Both Kurdish varieties"),
                ("all", "All source languages"),
            )
        ]
        registry = config.path(config.data.processed) / "corpora.json"
        if registry.is_file() and registry.stat().st_size < 1024 * 1024:
            try:
                for corpus in json.loads(registry.read_text(encoding="utf-8"))["corpora"]:
                    tag, records = corpus["language"], corpus["records"]
                    if not isinstance(tag, str) or type(records) is not int or records < 1:
                        continue
                    if not tag or LANGUAGE.fullmatch(tag):
                        corpora.append({"language": tag, "records": records})
                        if tag not in {"ckb", "kmr", ""}:
                            languages.append({"id": tag, "name": tag, "records": records})
            except OSError, ValueError, TypeError, KeyError:
                pass
        return {
            "version": __version__,
            "output_root": str(studio.output_root),
            "output_locations": [
                {
                    "id": name,
                    "label": "Dataset storage"
                    if name == "default"
                    else name.replace("-", " ").capitalize(),
                }
                for name in studio.output_locations
            ],
            "fonts_count": len(studio.fonts()),
            "raqm": features.check_feature("raqm"),
            "languages": languages,
            "corpora": corpora,
            "storage": studio.storage(),
            "upload_limit_bytes": UPLOAD_BYTES,
            "upload_formats": ["txt", "jsonl", "csv"],
            "document_presets": DOCUMENT_PRESETS,
            "document_layers": [
                {"id": name, "label": label, "description": description}
                for name, (label, description) in DOCUMENT_LAYER_LABELS.items()
            ],
            "image_presets": IMAGE_PRESETS,
            "recommended_preset": "balanced",
            "defaults": JobRequest().model_dump(exclude={"name", "source", "text", "upload_id"}),
        }

    @app.get("/api/directories")
    def directories(
        location: str = Query("default", max_length=32),
        path: str = Query("", max_length=512),
    ):
        return studio.directories(location, path)

    @app.post("/api/directories", status_code=201)
    def create_directory(request: DirectoryRequest):
        return studio.create_directory(request)

    @app.get("/api/fonts")
    def list_fonts():
        return {"fonts": studio.font_catalog()[0]}

    @app.post("/api/fonts", status_code=201)
    async def add_font(request: Request, filename: str = Query(..., min_length=1, max_length=255)):
        return await studio.add_font(request, filename)

    @app.delete("/api/fonts/{digest}")
    def remove_font(digest: str = FastAPIPath(pattern=r"^[a-f0-9]{64}$")):
        return studio.remove_font(digest)

    @app.post("/api/uploads", status_code=201)
    async def upload(
        request: Request,
        filename: str = Query(..., min_length=1, max_length=255),
        language: str = "sorani",
        languages: Annotated[
            list[Literal["ckb", "kmr", "ar", "en"]] | None, Query(min_length=1, max_length=4)
        ] = None,
    ):
        return await studio.upload(request, filename, language, languages)

    @app.delete("/api/uploads/{identifier}")
    def remove_upload(identifier: str):
        return studio.remove_upload(identifier)

    @app.post("/api/preview")
    def preview(request: PreviewRequest):
        return studio.preview(request)

    @app.post("/api/capacity")
    def capacity(request: CapacityRequest):
        return studio.capacity(request)

    @app.post("/api/jobs", status_code=202)
    def start(request: JobRequest):
        return studio.start(request)

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": studio.jobs()}

    @app.get("/api/jobs/{identifier}")
    def job(identifier: str):
        return studio.job(identifier)

    @app.post("/api/jobs/{identifier}/cancel")
    def cancel(identifier: str):
        return studio.cancel(identifier)

    @app.get("/api/jobs/{identifier}/samples")
    def samples(identifier: str, offset: int = Query(0, ge=0), limit: int = Query(12, ge=1, le=24)):
        return studio.samples(studio.job(identifier), offset, limit)

    @app.get("/api/jobs/{identifier}/images/{filename}")
    def image(identifier: str, filename: str):
        path = studio.image_path(studio.job(identifier), filename)
        if path.suffix == ".tif":
            # Browsers cannot display TIFF consistently; keep the original file in downloads.
            with Image.open(path) as source:
                if source.width * source.height > 16_777_216:
                    raise HTTPException(400, "Image exceeds the supported preview size")
                with io.BytesIO() as buffer:
                    source.save(buffer, format="PNG")
                    return Response(buffer.getvalue(), media_type="image/png")
        return FileResponse(path)

    @app.get("/api/jobs/{identifier}/thumbnails/{filename}")
    def thumbnail(identifier: str, filename: str):
        return studio.thumbnail(studio.job(identifier), filename)

    @app.post("/api/jobs/{identifier}/download")
    def prepare_download(
        identifier: str,
        format: DOWNLOAD_FORMAT = "dataset",
    ):
        # Check and prepare before triggering the native browser download. Errors
        # remain in the form instead of replacing it with a JSON error page.
        try:
            return studio.download(studio.job(identifier), format, prepare_only=True)
        except HTTPException as error:
            if error.status_code == 413:
                raise HTTPException(
                    413,
                    f"This ZIP exceeds the {studio.cache_limit // 1048576} MiB download limit. "
                    "Try fewer images or JPEG format in a new run. "
                    "You can still download the JSONL labels.",
                ) from error
            if error.status_code == 503:
                raise HTTPException(
                    503, "Other downloads are in progress. Try again after they finish."
                ) from error
            raise

    @app.get("/api/jobs/{identifier}/download")
    def download(
        identifier: str,
        format: DOWNLOAD_FORMAT = "dataset",
    ):
        return studio.download(studio.job(identifier), format)

    def ui_font(name: str):
        fonts = {
            "ui-latin.ttf": "fonts/open/notosans/NotoSans[wdth,wght].ttf",
            "ui-kurdish.ttf": "fonts/open/notonaskharabic/NotoNaskhArabic[wght].ttf",
        }
        if name not in fonts:
            raise HTTPException(404, "Font not found")
        path = config.path(fonts[name])
        if not path.is_file():
            raise HTTPException(404, "Font not installed")
        return FileResponse(path, media_type="font/ttf")

    @app.get("/assets/ui-latin.ttf")
    def latin_font():
        return ui_font("ui-latin.ttf")

    @app.get("/assets/ui-kurdish.ttf")
    def kurdish_font():
        return ui_font("ui-kurdish.ttf")

    static = Path(str(files("synthocr").joinpath("static")))
    if static.is_dir():
        app.mount("/assets", StaticFiles(directory=static), name="assets")

        @app.get("/")
        def index():
            return FileResponse(static / "index.html")

    return app
