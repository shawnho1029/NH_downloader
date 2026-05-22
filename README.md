# NH downloader

Downloads nhentai ZIPs, extracts them, reads `meta.json`, and archives each
gallery under `favorites/<artist>/`.

The artist folder is chosen from `meta.json` tags where `type == "artist"`.
If no artist tag exists, the script guesses from the first title bracket:

- `[Group (Artist)] Title` -> `Artist`
- `[Artist] Title` -> `Artist`
- no usable value -> `None`

## API key

Create `NH_API.md`:

```dotenv
API=your_api_key
```

If download or favorites sync returns 401/403, the API key is missing, invalid,
or expired.

## Interactive mode

```powershell
python nh_downloader.py
```

Menu:

1. Download pasted gallery URL
2. Sync and download favorites

Favorites mode first builds the records and queue, then asks before starting
downloads. It is intentionally slow by default:

- 3 seconds between favorites pages
- 10 seconds between gallery downloads
- 429 rate-limit responses wait and retry automatically

## Command line mode

Single gallery:

```powershell
python nh_downloader.py single https://nhentai.net/g/629366/
```

Backward-compatible shorthand:

```powershell
python nh_downloader.py 629366
```

Test extraction/archive using a ZIP already in Downloads:

```powershell
python nh_downloader.py single 629366 --import-from-downloads
```

Favorites:

```powershell
python nh_downloader.py favorites sync
python nh_downloader.py favorites run
python nh_downloader.py favorites sync-run
```

Useful test/throttle options:

```powershell
python nh_downloader.py favorites sync --max-pages 1
python nh_downloader.py favorites run --limit 3
python nh_downloader.py favorites sync-run --page-delay 5 --download-delay 20
```

## Records

Generated files live under `records/`:

- `favorites_list.json`: latest favorites snapshot
- `queue.json`: pending/done/failed/skipped queue
- `downloaded.json`: completed archive records
- `all.md`: human-readable full table
- `pending.md`: pending/downloading/failed items
- `downloaded.md`: done/skipped items

ZIP files are kept in `pending/archives/` after successful extraction by
default. Add `--delete-zip` to remove them after extraction.
