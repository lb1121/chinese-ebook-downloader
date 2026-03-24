---
name: chinese-ebook-downloader
description: >
  Download Chinese-language ebooks from multiple online sources. Primary source is a high-coverage
  online book library with no daily limit, using file hosting service for downloads.
  Secondary sources: other online book libraries, Z-Library. Handles file host password decryption,
  countdown wait, JS API real URL extraction, curl download, and GBK-encoded ZIP extraction automatically.
  Supports PDF, EPUB, MOBI, AZW3 formats.
  Use when the user says "下载电子书", "下载这本书", "找一下某本书的电子版",
  "帮我下个epub/mobi/azw3/pdf", "下载 XX 到电脑", or mentions a book title with
  implied intent to obtain a digital copy.
---

# Chinese Ebook Downloader

Download Chinese ebooks from online book libraries via file hosting services.

## Quick Start

```bash
# Download a single book
python scripts/download_book.py --title "超越百岁" --author "彼得·阿提亚"

# Download using known file host URL
python scripts/download_book.py --file-url "<file_host_url>" --title "超越百岁"

# Batch download from JSON
python scripts/batch_download.py --book-list books.json --output-dir ~/Books/
```

## Download Sources (Priority Order)

| Source | Coverage | Limit | Notes |
|-------|----------|-------|-------|
| **Source A** (online book library) | ~100% | None | Primary — high coverage for popular Chinese books |
| Source B (secondary library) | ~8% | None | Fallback for missing titles |
| Source C (Z-Library) | Medium | 10/day | Last resort |

### Book Name Matching Strategy

When a book title is long or contains multiple book names (e.g. box sets), the script automatically extracts core keywords for smarter searching:

- Removes subtitles (after "：" or ":")
- Removes parenthetical content ("（...）", "(...)")
- Removes "套装共X册" and similar bundle descriptions
- Splits "+"-connected titles into individual books
- Tries each extracted keyword in order until a match is found
- Falls back to full title + author

**Examples:**
- "杨定一全部生命系列：真原医+静坐+好睡（套装3册）" → tries "真原医", "静坐", "好睡"
- "超越百岁：长寿的科学与艺术" → tries "超越百岁", then "超越百岁 彼得·阿提亚"

## Workflow (Source A)

```
Search → Get file host link → Decrypt → Wait countdown → API fetch → curl download → Extract ZIP
```

### Step 1: Search

Search the primary online book library for the book title. Navigate to the download page and extract the file host URL and password.

### Step 2: Decrypt

Navigate browser to file host URL. Enter password and click decrypt button.

### Step 3: Wait for countdown

The file hosting service requires a countdown before allowing download. **Do not skip this.**

### Step 4: Fetch real download URL

**Get page variables:**
```javascript
JSON.stringify({api_server, userid, file_id, share_id, file_chk, start_time, wait_seconds, verifycode})
```

**Call API:**
```javascript
(async () => {
  var url = api_server + '/get_file_url.php?uid=' + userid
    + '&fid=' + file_id + '&folder_id=0&share_id=' + share_id
    + '&file_chk=' + file_chk + '&start_time=' + start_time
    + '&wait_seconds=' + wait_seconds + '&mb=0&app=0&acheck=0'
    + '&verifycode=' + verifycode + '&rd=' + Math.random();
  var headers = typeof getAjaxHeaders === 'function' ? getAjaxHeaders() : {};
  var resp = await fetch(url, {headers: headers});
  return JSON.stringify(await resp.json());
})()
```

Response `code: 200` → `downurl` is real URL, `file_size` is bytes.

### Step 5: Download

```bash
curl -L -o "book.zip" "DOWNURL" \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" \
  --max-time 1200
```

### Step 6: Extract ZIP (GBK encoding)

The file host ZIPs use GBK filenames. Use Python to extract:
```python
import zipfile
with zipfile.ZipFile('book.zip', 'r') as z:
    for info in z.infolist():
        try:
            name = info.filename.encode('cp437').decode('gbk')
        except:
            name = info.filename
        ext = os.path.splitext(name)[1].lower()
        if ext in ('.epub', '.azw3', '.mobi', '.pdf', '.txt'):
            data = z.read(info.filename)
            with open(os.path.basename(name), 'wb') as f:
                f.write(data)
```

## Batch Download

```bash
python scripts/batch_download.py --book-list books.json --output-dir ~/Books/
```

JSON format:
```json
[
  {"title": "超越百岁", "file_url": "<file_host_url>", "password": "<password>"}
]
```

Features: resume via `_progress.json`, skip existing files, rate limiting between downloads.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| IP blocking | Use browser tool, not web_fetch |
| Link 404 | Link expired, re-search |
| API non-200 | Re-navigate and re-decrypt |
| Download is HTML | URL expired, fresh API call needed |
| ZIP filenames garbled | Use Python cp437→gbk, not unzip |
| Timeout on large files | Increase `--max-time` to 1200 |
