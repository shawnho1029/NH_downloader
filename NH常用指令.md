# NH Downloader 常用指令

## 最簡單啟動

直接執行：

```powershell
.\run_nh_downloader.bat
```

批次檔只會問兩件事：

- 收藏頁範圍，預設 `1,10`
- 是否保留 ZIP，預設不保留

## 收藏同步並下載

```powershell
cd F:\code\Comic_downloader\NH_downloader
python nh_downloader.py favorites sync-run --page-range "1,10"
```

可用寫法：

```powershell
python nh_downloader.py favorites sync-run --page-range "1,10"
python nh_downloader.py favorites sync-run --page-range "1-10"
python nh_downloader.py favorites sync-run --pages-range "11,20"
```

## 只同步列表

```powershell
python nh_downloader.py favorites sync --page-range "1,10"
```

## 只下載現有 queue

```powershell
python nh_downloader.py favorites run
```

限制這次最多下載 3 本：

```powershell
python nh_downloader.py favorites run --limit 3
```

## 單本下載

```powershell
python nh_downloader.py single https://nhentai.net/g/629366/
python nh_downloader.py single 629366
```

## ZIP 行為

預設下載完成後不保留 ZIP。

要保留 ZIP：

```powershell
python nh_downloader.py favorites sync-run --page-range "1,10" --keep-zip
```

## 延遲與重試

通常不用調，預設已經偏保守：

```powershell
python nh_downloader.py favorites sync-run --page-range "1,10" --page-delay 3 --download-delay 20
python nh_downloader.py favorites sync-run --page-range "1,10" --retries 5 --retry-base 60 --max-retry-wait 100
```

## API Key

預設讀取 `NH_API.md`：

```dotenv
API=你的_api_key
```

也可以用環境變數或直接參數：

```powershell
$env:NHENTAI_API_KEY="你的_api_key"
python nh_downloader.py favorites sync-run --page-range "1,10"

python nh_downloader.py favorites sync-run --page-range "1,10" --api-key "你的_api_key"
```

## 說明

- `--page-range "1,10"`：抓第 1 到第 10 頁。
- `--max-pages 10`：舊用法，等於從第 1 頁抓到第 10 頁，保留作為相容用途。
- `.json` 是腳本真正讀寫的資料。
- `.md` 是方便人看的報表。
