from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.table import Table
except Exception:
    Console = None  # type: ignore[assignment]
    Progress = None  # type: ignore[assignment]


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://nhentai.net"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE_URL + "/",
}
INVALID_FILENAME_CHARS = '<>:"/\\|?*'
QUEUE_STATUSES = {"pending", "downloading", "done", "failed", "skipped"}
DEFAULT_PAGE_DELAY = 3.0
DEFAULT_DOWNLOAD_DELAY = 20.0
DEFAULT_RETRIES = 5
DEFAULT_RETRY_BASE = 60.0
DEFAULT_MAX_RETRY_WAIT = 100.0

console = Console() if Console is not None else None
err_console = Console(stderr=True) if Console is not None else None


def ui_print(message: Any = "", *, style: str | None = None, err: bool = False) -> None:
    if console is not None:
        target = err_console if err and err_console is not None else console
        target.print(message, style=style)
    else:
        print(message, file=sys.stderr if err else sys.stdout)


def ui_rule(title: str) -> None:
    if console is not None:
        console.rule(title)
    else:
        print(f"=== {title} ===")


class DownloadError(RuntimeError):
    pass


class AuthError(DownloadError):
    pass


class NetworkError(DownloadError):
    pass


class ParseError(DownloadError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def load_api_key(path: Path) -> str | None:
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.strip().strip('"').strip("'")
        if key in {"API", "NHENTAI_API_KEY"} and value:
            return value
    return None


def parse_page_range(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    parts = [part for part in re.split(r"\s*(?:,|~|-)\s*", value) if part]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("Page range must look like 1,10 or 1-10.")
    start, end = (int(parts[0]), int(parts[1]))
    if start < 1 or end < 1:
        raise ValueError("Page range must use positive page numbers.")
    if start > end:
        start, end = end, start
    return start, end


def safe_name(value: str, fallback: str = "None", max_length: int = 180) -> str:
    value = (value or "").strip()
    if not value:
        value = fallback
    table = str.maketrans({char: "_" for char in INVALID_FILENAME_CHARS})
    value = value.translate(table)
    value = re.sub(r"[\x00-\x1f]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if value.upper() in reserved:
        value = f"_{value}"
    return value[:max_length].rstrip(" .") or fallback


def gallery_id_from_url_or_id(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    match = re.search(r"/g/(\d+)/?", value)
    if not match:
        raise ValueError(f"Cannot find gallery id in: {value}")
    return int(match.group(1))


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.name} ({index})")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Cannot find an unused path for {path}")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def require_ok(response: requests.Response, action: str) -> None:
    if response.status_code in {401, 403}:
        raise AuthError(
            f"{action} failed: HTTP {response.status_code}. "
            "Auth is missing or expired. Check NH_API.md or NHENTAI_API_KEY."
        )
    if not response.ok:
        detail = response.text[:500].replace("\n", " ")
        raise DownloadError(f"{action} failed: HTTP {response.status_code} {detail}")


def retry_after_seconds(
    response: requests.Response,
    fallback: float,
    max_wait: float = DEFAULT_MAX_RETRY_WAIT,
) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return min(fallback, max_wait)
    try:
        return min(max(float(value), fallback), max_wait)
    except ValueError:
        return min(fallback, max_wait)


def apply_auth_headers(session: requests.Session, api_key: str | None = None) -> None:
    api_key = (
        api_key
        or getattr(session, "_api_key", None)
        or os.environ.get("NHENTAI_API_KEY")
        or os.environ.get("API")
    )
    session.headers.pop("Cookie", None)
    if not api_key:
        session.headers.pop("Authorization", None)
        return
    session.headers["Authorization"] = f"Key {api_key}"


def build_session(api_key: str | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session._api_key = api_key
    apply_auth_headers(session, api_key=api_key)
    return session


def maybe_refresh_auth(session: requests.Session, force: bool = False) -> None:
    env_path = getattr(session, "_env_path", None)
    api_key_path = getattr(session, "_api_key_path", None)
    interval = float(getattr(session, "_auth_refresh_interval", 0.0) or 0.0)
    last_refresh = float(getattr(session, "_auth_last_refresh", 0.0) or 0.0)
    now = time.time()
    should_refresh = force or (interval > 0 and (now - last_refresh) >= interval)
    if not should_refresh:
        return
    if env_path:
        load_dotenv(Path(env_path))
    if api_key_path:
        api_key = load_api_key(Path(api_key_path))
        if api_key:
            session._api_key = api_key
    apply_auth_headers(session)
    session._auth_last_refresh = now


def request_api(
    session: requests.Session,
    method: str,
    path: str,
    retries: int = DEFAULT_RETRIES,
    retry_base: float = DEFAULT_RETRY_BASE,
    max_retry_wait: float | None = None,
    **kwargs: Any,
) -> Any:
    url = f"{BASE_URL}{path}"
    action = method.upper() + " " + path
    if max_retry_wait is None:
        max_retry_wait = float(
            getattr(session, "_max_retry_wait", DEFAULT_MAX_RETRY_WAIT)
        )
    for attempt in range(retries + 1):
        maybe_refresh_auth(session)
        try:
            response = session.request(method, url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            if attempt < retries:
                wait = min(retry_base * (attempt + 1), 30)
                ui_print(
                    f"Network error on {action}. Waiting {wait:.0f}s before retry {attempt + 1}/{retries}..."
                )
                time.sleep(wait)
                continue
            raise NetworkError(f"{action} network failed: {exc}") from exc
        if response.status_code == 429 and attempt < retries:
            wait = retry_after_seconds(
                response, retry_base * (attempt + 1), max_wait=max_retry_wait
            )
            ui_print(
                f"Rate limited on {action}. Waiting {wait:.0f}s before retry {attempt + 1}/{retries}..."
            )
            time.sleep(wait)
            continue
        if response.status_code in {500, 502, 503, 504} and attempt < retries:
            wait = min(retry_base, 5 * (attempt + 1))
            ui_print(
                f"Server error HTTP {response.status_code} on {action}. Waiting {wait:.0f}s..."
            )
            time.sleep(wait)
            continue
        if response.status_code in {401, 403} and attempt < retries:
            ui_print(f"Auth failed on {action}. Reloading API key and retrying...")
            maybe_refresh_auth(session, force=True)
            time.sleep(1)
            continue
        require_ok(response, action)
        return response.json()
    raise NetworkError(f"{action} failed after retries")


def request_download_url(
    session: requests.Session, gallery_id: int, fmt: str = "zip"
) -> str:
    data = request_api(
        session,
        "POST",
        f"/api/v2/galleries/{gallery_id}/download",
        params={"format": fmt},
    )
    url = data.get("url")
    if not isinstance(url, str) or not url:
        raise DownloadError(f"download request did not return a url: {data!r}")
    return url


def download_file(
    session: requests.Session,
    url: str,
    target_dir: Path,
    gallery_id: int,
    retries: int = DEFAULT_RETRIES,
    retry_base: float = DEFAULT_RETRY_BASE,
    max_retry_wait: float | None = None,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    parsed_name = Path(urlparse(url).path).name
    filename = (
        parsed_name
        if parsed_name.lower().endswith(".zip")
        else f"nhentai-{gallery_id}.zip"
    )
    target = unique_path(
        target_dir / safe_name(filename, f"nhentai-{gallery_id}.zip", max_length=240)
    )

    tmp = target.with_suffix(target.suffix + ".part")
    if max_retry_wait is None:
        max_retry_wait = float(
            getattr(session, "_max_retry_wait", DEFAULT_MAX_RETRY_WAIT)
        )
    for attempt in range(retries + 1):
        maybe_refresh_auth(session)
        try:
            with session.get(url, stream=True, timeout=60) as response:
                if response.status_code == 429 and attempt < retries:
                    wait = retry_after_seconds(
                        response, retry_base * (attempt + 1), max_wait=max_retry_wait
                    )
                    ui_print(
                        f"Rate limited while downloading #{gallery_id}. Waiting {wait:.0f}s..."
                    )
                    time.sleep(wait)
                    continue
                if response.status_code in {500, 502, 503, 504} and attempt < retries:
                    wait = min(retry_base, 5 * (attempt + 1))
                    ui_print(
                        f"Download server error HTTP {response.status_code} for #{gallery_id}. Waiting {wait:.0f}s..."
                    )
                    time.sleep(wait)
                    continue
                if response.status_code in {401, 403} and attempt < retries:
                    ui_print(
                        f"Auth failed while downloading #{gallery_id}. Reloading API key and retrying..."
                    )
                    maybe_refresh_auth(session, force=True)
                    time.sleep(1)
                    continue
                require_ok(response, "zip download")
                total = int(response.headers.get("content-length") or 0)
                with tmp.open("wb") as handle:
                    if Progress is None:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                handle.write(chunk)
                    else:
                        with Progress(
                            SpinnerColumn(),
                            TextColumn(f"[bold]Downloading #{gallery_id}"),
                            BarColumn(),
                            DownloadColumn(),
                            TransferSpeedColumn(),
                            TimeRemainingColumn(),
                            console=console,
                        ) as progress:
                            task = progress.add_task("download", total=total or None)
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    handle.write(chunk)
                                    progress.update(task, advance=len(chunk))
                tmp.replace(target)
                return target
        except requests.RequestException as exc:
            if attempt < retries:
                wait = min(retry_base * (attempt + 1), 30)
                ui_print(
                    f"Network error while downloading #{gallery_id}. Waiting {wait:.0f}s..."
                )
                time.sleep(wait)
                continue
            raise NetworkError(
                f"zip download network failed for #{gallery_id}: {exc}"
            ) from exc
    raise NetworkError(f"zip download failed after retries for #{gallery_id}")


def import_existing_zip(
    gallery_id: int, pending_dir: Path, downloads_dir: Path | None = None
) -> Path:
    downloads_dir = downloads_dir or Path.home() / "Downloads"
    matches = sorted(
        downloads_dir.glob(f"nhentai-{gallery_id}*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise FileNotFoundError(
            f"No nhentai-{gallery_id}*.zip found in {downloads_dir}"
        )
    pending_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(pending_dir / matches[0].name)
    shutil.copy2(matches[0], target)
    return target


def extract_zip_to_same_named_folder(zip_path: Path, extract_root: Path) -> Path:
    extract_root.mkdir(parents=True, exist_ok=True)
    folder = unique_path(extract_root / zip_path.stem)
    folder.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(folder)

    children = [child for child in folder.iterdir()]
    if len(children) == 1 and children[0].is_dir():
        inner = children[0]
        if (inner / "meta.json").exists():
            flattened = unique_path(folder.with_name(inner.name))
            folder.rmdir()
            inner.rename(flattened)
            return flattened
    return folder


def read_meta(manga_dir: Path) -> dict[str, Any]:
    meta_path = manga_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"meta.json not found under {manga_dir}")
    return json.loads(meta_path.read_text(encoding="utf-8-sig"))


def title_from_meta(meta: dict[str, Any]) -> str:
    title = meta.get("title")
    if isinstance(title, dict):
        for key in ("english", "pretty", "japanese"):
            value = title.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(title, str):
        return title.strip()
    return f"nhentai-{meta.get('id', 'unknown')}"


def artists_from_meta(meta: dict[str, Any]) -> list[str]:
    tags = meta.get("tags") or []
    artists = []
    for tag in tags:
        if isinstance(tag, dict) and tag.get("type") == "artist" and tag.get("name"):
            artists.append(str(tag["name"]).strip())
    return [artist for artist in artists if artist]


def artist_from_title(title: str) -> str | None:
    first_bracket = re.match(r"^\s*\[([^\]]+)\]", title)
    if first_bracket:
        credit = first_bracket.group(1).strip()
        paren = re.search(r"\(([^()]+)\)\s*$", credit)
        if paren:
            return paren.group(1).strip()
        if "-" in credit:
            return credit.split("-", 1)[0].strip() or None
        return credit or None
    plain = title.strip()
    if "-" in plain:
        left = plain.split("-", 1)[0].strip()
        if left:
            return left
    return None


def classify_error(exc: Exception) -> str:
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, AuthError):
        return f"auth_error: {text}"
    if "ssl" in lower or "certificate" in lower:
        return f"ssl_error: {text}"
    if "timed out" in lower or "timeout" in lower:
        return f"timeout_error: {text}"
    if "http 429" in lower or "rate limit" in lower:
        return f"rate_limit_error: {text}"
    if isinstance(exc, ParseError):
        return f"parse_error: {text}"
    if isinstance(exc, NetworkError):
        return f"network_error: {text}"
    return f"unknown_error: {text}"


def print_queue_status(index: int, total: int, gallery_id: int, title: str) -> None:
    ui_rule(f"Gallery {index}/{total}")
    ui_print(f"#{gallery_id} {title}", style="bold cyan")


def destination_artist(meta: dict[str, Any]) -> str:
    artists = artists_from_meta(meta)
    if artists:
        return " & ".join(artists)
    guessed = artist_from_title(title_from_meta(meta))
    return guessed or "None"


def archive_folder_name(meta: dict[str, Any]) -> str:
    gallery_id = meta.get("id", "unknown")
    title = title_from_meta(meta).replace(" | ", " - ").strip()
    return f"nhentai-{gallery_id} - {title}"


def archive_manga_folder(manga_dir: Path, favorites_dir: Path) -> Path:
    meta = read_meta(manga_dir)
    artist = safe_name(destination_artist(meta))
    artist_dir = favorites_dir / artist
    artist_dir.mkdir(parents=True, exist_ok=True)
    folder_name = safe_name(
        archive_folder_name(meta), f"nhentai-{meta.get('id', 'unknown')}"
    )
    target = unique_path(artist_dir / folder_name)
    shutil.move(str(manga_dir), str(target))
    return target


def process_zip(
    zip_path: Path, pending_dir: Path, favorites_dir: Path, keep_zip: bool = True
) -> Path:
    extracted_dir = extract_zip_to_same_named_folder(
        zip_path, pending_dir / "extracted"
    )
    archived_dir = archive_manga_folder(extracted_dir, favorites_dir)
    if keep_zip:
        archive_dir = pending_dir / "archives"
        archive_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(zip_path), str(unique_path(archive_dir / zip_path.name)))
    else:
        zip_path.unlink(missing_ok=True)
    return archived_dir


def root_paths(root: Path) -> dict[str, Path]:
    root = root.resolve()
    records = root / "records"
    return {
        "root": root,
        "pending": root / "pending",
        "favorites": root / "favorites",
        "records": records,
        "favorites_list": records / "favorites_list.json",
        "queue": records / "queue.json",
        "downloaded": records / "downloaded.json",
        "all_md": records / "all.md",
        "pending_md": records / "pending.md",
        "downloaded_md": records / "downloaded.md",
    }


def gallery_record(raw: dict[str, Any], page: int) -> dict[str, Any]:
    title = (
        raw.get("english_title")
        or raw.get("japanese_title")
        or f"nhentai-{raw.get('id')}"
    )
    gallery_id = int(raw["id"])
    return {
        "id": gallery_id,
        "url": f"{BASE_URL}/g/{gallery_id}/",
        "title": title,
        "japanese_title": raw.get("japanese_title") or "",
        "page": page,
        "thumbnail": raw.get("thumbnail") or "",
        "num_pages": raw.get("num_pages") or 0,
        "media_id": raw.get("media_id") or "",
    }


def fetch_favorites(
    session: requests.Session,
    query: str = "",
    delay: float = DEFAULT_PAGE_DELAY,
    max_pages: int | None = None,
    page_range: tuple[int, int] | None = None,
    retries: int = DEFAULT_RETRIES,
    retry_base: float = DEFAULT_RETRY_BASE,
) -> list[dict[str, Any]]:
    all_items: list[dict[str, Any]] = []
    seen: set[int] = set()
    range_start = page_range[0] if page_range else 1
    range_end: int | None = page_range[1] if page_range else max_pages
    page = range_start
    num_pages = 1
    range_label = f"{range_start}-{range_end}" if range_end else f"{range_start}-all"
    ui_rule(f"Sync favorites pages {range_label}")
    while (range_end is None or page <= range_end) and page <= max(num_pages, range_start):
        data = request_api(
            session,
            "GET",
            "/api/v2/favorites",
            params={"page": page, "q": query},
            retries=retries,
            retry_base=retry_base,
        )
        num_pages = int(data.get("num_pages") or 1)
        for raw in data.get("result") or []:
            gallery_id = int(raw["id"])
            if gallery_id in seen:
                continue
            seen.add(gallery_id)
            all_items.append(gallery_record(raw, page))
        ui_print(
            f"Fetched favorites page {page}/{num_pages} ({len(all_items)} total)",
            style="green",
        )
        page += 1
        if (range_end is None or page <= range_end) and page <= num_pages and delay > 0:
            time.sleep(delay)
    return all_items


def scan_existing_archives(favorites_dir: Path) -> dict[int, dict[str, Any]]:
    existing: dict[int, dict[str, Any]] = {}
    if not favorites_dir.exists():
        return existing
    for meta_path in favorites_dir.rglob("meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            gallery_id = int(meta["id"])
        except Exception:
            continue
        manga_dir = meta_path.parent
        existing[gallery_id] = {
            "id": gallery_id,
            "title": title_from_meta(meta),
            "artist": destination_artist(meta),
            "path": str(manga_dir),
            "completed_at": utc_now(),
        }
    return existing


def downloaded_map(paths: dict[str, Path]) -> dict[int, dict[str, Any]]:
    downloaded = read_json(paths["downloaded"], [])
    scanned = scan_existing_archives(paths["favorites"])
    for item in downloaded:
        if "id" not in item:
            continue
        gallery_id = int(item["id"])
        if gallery_id in scanned:
            scanned[gallery_id]["completed_at"] = (
                item.get("completed_at") or scanned[gallery_id]["completed_at"]
            )
        else:
            scanned[gallery_id] = item
    return scanned


def save_downloaded(
    paths: dict[str, Path], records_by_id: dict[int, dict[str, Any]]
) -> None:
    records = sorted(records_by_id.values(), key=lambda item: int(item["id"]))
    write_json(paths["downloaded"], records)


def build_queue(
    favorites: list[dict[str, Any]], paths: dict[str, Path]
) -> list[dict[str, Any]]:
    existing_downloaded = downloaded_map(paths)
    old_queue = read_json(paths["queue"], [])
    old_by_id = {int(item["id"]): item for item in old_queue if "id" in item}
    queue: list[dict[str, Any]] = []
    for item in favorites:
        gallery_id = int(item["id"])
        previous = old_by_id.get(gallery_id, {})
        if gallery_id in existing_downloaded:
            status = "skipped" if previous.get("status") != "done" else "done"
            path = existing_downloaded[gallery_id].get("path", "")
            error = ""
        else:
            status = (
                previous.get("status")
                if previous.get("status") in QUEUE_STATUSES
                else "pending"
            )
            if status in {"done", "skipped"}:
                status = "pending"
            path = previous.get("path", "")
            error = previous.get("error", "")
        queue.append(
            {
                **item,
                "status": status,
                "path": path,
                "error": error,
                "updated_at": previous.get("updated_at") or utc_now(),
            }
        )
    return queue


def queue_summary(queue: list[dict[str, Any]]) -> dict[str, int]:
    summary = {status: 0 for status in QUEUE_STATUSES}
    for item in queue:
        status = item.get("status", "pending")
        summary[status] = summary.get(status, 0) + 1
    summary["total"] = len(queue)
    return summary


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def markdown_table(items: list[dict[str, Any]], title: str) -> str:
    lines = [
        f"# {title}",
        "",
        f"Generated: {utc_now()}",
        "",
        "| ID | Status | Title | Path/Error |",
        "| --- | --- | --- | --- |",
    ]
    for item in items:
        detail = item.get("path") or item.get("error") or ""
        lines.append(
            f"| [{item['id']}]({item['url']}) | {md_escape(item.get('status', ''))} | "
            f"{md_escape(item.get('title', ''))} | {md_escape(detail)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_records(
    paths: dict[str, Path], favorites: list[dict[str, Any]], queue: list[dict[str, Any]]
) -> None:
    write_json(paths["favorites_list"], favorites)
    write_json(paths["queue"], queue)
    downloaded = downloaded_map(paths)
    save_downloaded(paths, downloaded)
    write_text(paths["all_md"], markdown_table(queue, "All favorites"))
    pending_items = [
        item
        for item in queue
        if item.get("status") in {"pending", "downloading", "failed"}
    ]
    write_text(paths["pending_md"], markdown_table(pending_items, "Pending favorites"))
    done_items = [item for item in queue if item.get("status") in {"done", "skipped"}]
    write_text(
        paths["downloaded_md"], markdown_table(done_items, "Downloaded favorites")
    )


def sync_favorites(
    root: Path,
    session: requests.Session,
    query: str = "",
    page_delay: float = DEFAULT_PAGE_DELAY,
    max_pages: int | None = None,
    page_range: tuple[int, int] | None = None,
    retries: int = DEFAULT_RETRIES,
    retry_base: float = DEFAULT_RETRY_BASE,
) -> list[dict[str, Any]]:
    paths = root_paths(root)
    favorites = fetch_favorites(
        session,
        query=query,
        delay=page_delay,
        max_pages=max_pages,
        page_range=page_range,
        retries=retries,
        retry_base=retry_base,
    )
    queue = build_queue(favorites, paths)
    write_records(paths, favorites, queue)
    summary = queue_summary(queue)
    if console is not None:
        table = Table(title="Favorites Synced")
        table.add_column("Total", justify="right")
        table.add_column("Pending", justify="right")
        table.add_column("Done", justify="right")
        table.add_column("Skipped", justify="right")
        table.add_column("Failed", justify="right")
        table.add_row(
            str(summary["total"]),
            str(summary.get("pending", 0)),
            str(summary.get("done", 0)),
            str(summary.get("skipped", 0)),
            str(summary.get("failed", 0)),
        )
        console.print(table)
    else:
        ui_print(
            "Favorites synced: "
            f"total={summary['total']}, pending={summary.get('pending', 0)}, "
            f"done={summary.get('done', 0)}, skipped={summary.get('skipped', 0)}, "
            f"failed={summary.get('failed', 0)}"
        )
    return queue


def mark_queue_item(
    paths: dict[str, Path], gallery_id: int, **updates: Any
) -> dict[str, Any]:
    queue = read_json(paths["queue"], [])
    changed: dict[str, Any] = {}
    for item in queue:
        if int(item.get("id", -1)) == gallery_id:
            item.update(updates)
            item["updated_at"] = utc_now()
            changed = item
            break
    write_json(paths["queue"], queue)
    write_records(paths, read_json(paths["favorites_list"], []), queue)
    return changed


def record_downloaded(
    paths: dict[str, Path], gallery_id: int, archived_dir: Path
) -> dict[str, Any]:
    meta = read_meta(archived_dir)
    record = {
        "id": gallery_id,
        "title": title_from_meta(meta),
        "artist": destination_artist(meta),
        "path": str(archived_dir),
        "completed_at": utc_now(),
    }
    records = downloaded_map(paths)
    records[gallery_id] = record
    save_downloaded(paths, records)
    return record


def download_gallery(
    gallery_id: int,
    root: Path,
    session: requests.Session,
    import_from_downloads: bool = False,
    downloads_dir: Path | None = None,
    delete_zip: bool = False,
) -> Path:
    paths = root_paths(root)
    if import_from_downloads:
        zip_path = import_existing_zip(gallery_id, paths["pending"], downloads_dir)
    else:
        download_url = request_download_url(session, gallery_id, "zip")
        zip_path = download_file(session, download_url, paths["pending"], gallery_id)
    archived_dir = process_zip(
        zip_path, paths["pending"], paths["favorites"], keep_zip=not delete_zip
    )
    record_downloaded(paths, gallery_id, archived_dir)
    return archived_dir


def run_queue(
    root: Path,
    session: requests.Session,
    limit: int | None = None,
    delete_zip: bool = False,
    download_delay: float = DEFAULT_DOWNLOAD_DELAY,
) -> None:
    paths = root_paths(root)
    queue = read_json(paths["queue"], [])
    if not queue:
        raise RuntimeError("queue.json is empty. Run favorites sync first.")
    remaining = [
        item
        for item in queue
        if item.get("status") in {"pending", "failed", "downloading"}
    ]
    if limit is not None:
        remaining = remaining[:limit]
    ui_print(f"Starting queue: {len(remaining)} item(s)", style="bold")
    for index, item in enumerate(remaining, start=1):
        gallery_id = int(item["id"])
        print_queue_status(index, len(remaining), gallery_id, item.get("title", ""))
        try:
            mark_queue_item(paths, gallery_id, status="downloading", error="")
            archived_dir = download_gallery(
                gallery_id, root, session, delete_zip=delete_zip
            )
            mark_queue_item(
                paths, gallery_id, status="done", path=str(archived_dir), error=""
            )
            ui_print(f"Done: {archived_dir}", style="green")
        except Exception as exc:
            classified = classify_error(exc)
            mark_queue_item(paths, gallery_id, status="failed", error=classified)
            ui_print(f"Failed #{gallery_id}: {classified}", style="red", err=True)
        if index < len(remaining) and download_delay > 0:
            ui_print(f"Waiting {download_delay:.0f}s before next gallery...")
            time.sleep(download_delay)


def run_single(args: argparse.Namespace, session: requests.Session) -> Path:
    gallery_id = gallery_id_from_url_or_id(args.gallery)
    archived_dir = download_gallery(
        gallery_id,
        Path(args.root),
        session,
        import_from_downloads=args.import_from_downloads,
        downloads_dir=Path(args.downloads_dir).expanduser(),
        delete_zip=args.delete_zip,
    )
    ui_print(f"Archived to: {archived_dir}", style="green")
    return archived_dir


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{suffix}] ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "1", "true"}


def interactive(root: Path, session: requests.Session, args: argparse.Namespace) -> int:
    ui_print("1. Download pasted gallery URL")
    ui_print("2. Sync and download favorites")
    choice = input("Choose 1 or 2: ").strip()
    if choice == "1":
        gallery = input("Paste gallery URL or id: ").strip()
        if not gallery:
            ui_print("No gallery provided.", style="red", err=True)
            return 1
        args.gallery = gallery
        run_single(args, session)
        return 0
    if choice == "2":
        queue = sync_favorites(
            root,
            session,
            query=args.query,
            page_delay=args.page_delay,
            max_pages=args.max_pages,
            page_range=parse_page_range(getattr(args, "page_range", None)),
            retries=args.retries,
            retry_base=args.retry_base,
        )
        summary = queue_summary(queue)
        ui_print(
            f"Queue ready: {summary['total']} total, "
            f"{summary.get('pending', 0) + summary.get('failed', 0)} to download."
        )
        if confirm("Start downloading now?"):
            run_queue(
                root,
                session,
                limit=args.limit,
                delete_zip=args.delete_zip,
                download_delay=args.download_delay,
            )
        return 0
    ui_print("Unknown choice.", style="red", err=True)
    return 1


def add_common_args(
    parser: argparse.ArgumentParser, suppress_defaults: bool = False
) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--root",
        default=argparse.SUPPRESS if suppress_defaults else ".",
        help="Project data root. Defaults to current directory.",
    )
    parser.add_argument(
        "--env-file",
        default=argparse.SUPPRESS if suppress_defaults else ".env",
        help="Local env file. Defaults to .env.",
    )
    parser.add_argument(
        "--api-file",
        default=argparse.SUPPRESS if suppress_defaults else "NH_API.md",
        help="Local API key file. Defaults to NH_API.md.",
    )
    parser.add_argument(
        "--api-key",
        default=default,
        help="nhentai API key. Can also use NHENTAI_API_KEY or API.",
    )
    parser.add_argument(
        "--delete-zip",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else True,
        help="Delete the ZIP after successful extraction (default behavior).",
    )
    parser.add_argument(
        "--keep-zip",
        dest="delete_zip",
        action="store_false",
        default=argparse.SUPPRESS if suppress_defaults else True,
        help="Keep the ZIP after successful extraction.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_RETRIES,
        help=f"Retries for 429/temporary server errors. Defaults to {DEFAULT_RETRIES}.",
    )
    parser.add_argument(
        "--retry-base",
        type=float,
        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_RETRY_BASE,
        help=f"Base wait seconds for 429 retry. Defaults to {DEFAULT_RETRY_BASE:g}.",
    )
    parser.add_argument(
        "--max-retry-wait",
        type=float,
        default=argparse.SUPPRESS if suppress_defaults else DEFAULT_MAX_RETRY_WAIT,
        help=f"Cap seconds for each 429 retry wait. Defaults to {DEFAULT_MAX_RETRY_WAIT:g}.",
    )
    parser.add_argument(
        "--auth-refresh-interval",
        type=float,
        default=argparse.SUPPRESS if suppress_defaults else 120.0,
        help="Seconds between automatic auth reload from .env/NH_API.md during long runs. Defaults to 120.",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download nhentai ZIPs and archive them by artist."
    )
    add_common_args(parser)
    parser.add_argument("--query", default="", help="Favorites search query.")
    parser.add_argument(
        "--limit", type=int, help="Maximum queue items to download in this run."
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=DEFAULT_PAGE_DELAY,
        help=f"Seconds between favorites pages. Defaults to {DEFAULT_PAGE_DELAY:g}.",
    )
    parser.add_argument(
        "--download-delay",
        type=float,
        default=DEFAULT_DOWNLOAD_DELAY,
        help=f"Seconds between queue downloads. Defaults to {DEFAULT_DOWNLOAD_DELAY:g}.",
    )
    parser.add_argument(
        "--max-pages", type=int, help="Favorites sync page cap for testing."
    )
    parser.add_argument(
        "--page-range",
        "--pages-range",
        dest="page_range",
        help="Favorites page range, e.g. 1,10 or 1-10.",
    )
    parser.add_argument(
        "--import-from-downloads", action="store_true", help="For single mode only."
    )
    parser.add_argument("--downloads-dir", default=str(Path.home() / "Downloads"))

    subparsers = parser.add_subparsers(dest="command")
    single = subparsers.add_parser("single", help="Download one gallery by URL or id.")
    add_common_args(single, suppress_defaults=True)
    single.add_argument("gallery")
    single.add_argument(
        "--import-from-downloads", action="store_true", default=argparse.SUPPRESS
    )
    single.add_argument("--downloads-dir", default=argparse.SUPPRESS)

    favorites = subparsers.add_parser("favorites", help="Sync or run favorites queue.")
    add_common_args(favorites, suppress_defaults=True)
    favorites.add_argument("action", choices=["sync", "run", "sync-run"])
    favorites.add_argument("--query", default=argparse.SUPPRESS)
    favorites.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    favorites.add_argument("--page-delay", type=float, default=argparse.SUPPRESS)
    favorites.add_argument("--download-delay", type=float, default=argparse.SUPPRESS)
    favorites.add_argument("--max-pages", type=int, default=argparse.SUPPRESS)
    favorites.add_argument(
        "--page-range",
        "--pages-range",
        dest="page_range",
        default=argparse.SUPPRESS,
    )

    # Backward compatibility: python nh_downloader.py 629366
    if (
        argv
        and argv[0] not in {"single", "favorites", "-h", "--help"}
        and not argv[0].startswith("-")
    ):
        argv = ["single", *argv]
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = parse_args(argv)
    root = Path(args.root)
    load_dotenv(root / args.env_file)
    api_key_path = root / args.api_file
    api_key = args.api_key or load_api_key(api_key_path)
    if not api_key and not os.environ.get("NHENTAI_API_KEY") and not os.environ.get("API"):
        raise RuntimeError(
            f"Missing API key. Put API=... in {api_key_path} or set NHENTAI_API_KEY."
        )
    session = build_session(api_key=api_key)
    session._env_path = str(root / args.env_file)
    session._api_key_path = str(api_key_path)
    session._auth_refresh_interval = max(0.0, float(args.auth_refresh_interval))
    session._max_retry_wait = max(1.0, float(args.max_retry_wait))
    session._auth_last_refresh = 0.0
    try:
        if args.command == "single":
            run_single(args, session)
        elif args.command == "favorites":
            page_range = parse_page_range(getattr(args, "page_range", None))
            if args.action in {"sync", "sync-run"}:
                sync_favorites(
                    root,
                    session,
                    query=args.query,
                    page_delay=args.page_delay,
                    max_pages=args.max_pages,
                    page_range=page_range,
                    retries=args.retries,
                    retry_base=args.retry_base,
                )
            if args.action in {"run", "sync-run"}:
                run_queue(
                    root,
                    session,
                    limit=args.limit,
                    delete_zip=args.delete_zip,
                    download_delay=args.download_delay,
                )
        else:
            return interactive(root, session, args)
    except Exception as exc:
        ui_print(f"ERROR: {exc}", style="red", err=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
