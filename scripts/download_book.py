#!/usr/bin/env python3
"""
Download Chinese ebooks from online book library via file hosting service.

Primary source: Online book library (high coverage)
Secondary sources: other online book libraries, Z-Library

Usage:
    python download_book.py --title "书名" [--author "作者"] [--output-dir ~/Downloads]
    python download_book.py --ctfile-url "<file_host_url>" --title "书名"

Workflow:
    1. Search online book library for the title
    2. Navigate to content / download page
    3. Extract file host link + password
    4. Browser decrypt (input password, click decrypt)
    5. Wait for countdown
    6. Get API variables and call download URL API
    7. Download with curl
    8. Extract ZIP with GBK encoding fix (cp437→gbk)
"""
import sys
import os
import re
import json
import asyncio
import subprocess
import zipfile
import tempfile
import shutil
from pathlib import Path
from urllib.parse import quote, urljoin
from typing import Optional

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Error: Install playwright: pip install playwright && playwright install chromium")
    sys.exit(1)

# Constants
SOURCE_A_BASE_URL = os.environ.get("SOURCE_A_BASE_URL", "https://dushupai.com")
FILE_HOST_BASE_URL = os.environ.get("FILE_HOST_BASE_URL", "https://z701.com")
DEFAULT_PASSWORD = os.environ.get("EBOOK_DEFAULT_PASSWORD", "")
DOWNLOAD_TIMEOUT = 1200  # 20 minutes for large files
WAIT_SECONDS = 60  # File host requires 60 second wait


def sanitize_filename(name: str) -> str:
    """Clean filename for filesystem."""
    name = re.sub(r'[\[\]【】（）()《》]', '', name)
    name = re.sub(r'[：:]', ' -', name)
    name = re.sub(r'[\\/:*?"<>|]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:200]


async def search_primary_source(title: str, author: str = "", headless: bool = True) -> list[dict]:
    """
    Search primary book source and return results with file host links.

    Returns list of dicts with: title, id, file_url, pwd
    """
    query = f"{title} {author}".strip()
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        # Search
        search_url = f"{SOURCE_A_BASE_URL}/?s={quote(query)}"
        print(f"Searching primary source for: {query}")
        await page.goto(search_url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(2)

        # Get search results
        items = await page.query_selector_all('article a, .post-title a, h2 a, .entry-title a')

        if not items:
            print("No search results found")
            await browser.close()
            return []

        # Check first 5 results
        for item in items[:5]:
            try:
                item_title = (await item.inner_text()).strip()
                item_href = await item.get_attribute('href')

                if not item_href or not item_title:
                    continue

                # Check if it's a book content page
                if '/book-content-' not in item_href and '/post/' not in item_href:
                    continue

                full_url = urljoin(SOURCE_A_BASE_URL, item_href)

                # Extract book ID from URL
                id_match = re.search(r'book-content-(\d+)', full_url)
                if not id_match:
                    id_match = re.search(r'/post/(\d+)', full_url)
                book_id = id_match.group(1) if id_match else ""

                print(f"  Checking: {item_title[:50]}...")
                await page.goto(full_url, wait_until='domcontentloaded', timeout=20000)
                await asyncio.sleep(2)

                # Find download link
                ctfile_url, pwd = await _extract_file_host_link(page, book_id)

                if ctfile_url:
                    print(f"  Found file host link (password: {pwd})")
                    results.append({
                        "title": item_title,
                        "id": book_id,
                        "ctfile_url": ctfile_url,
                        "pwd": pwd
                    })
                    break  # Use first match
            except Exception as e:
                print(f"  Warning: {e}")
                continue

        await browser.close()

    return results


async def _extract_file_host_link(page, book_id: str) -> tuple[str, str]:
    """
    Extract file host link from book page.
    May need to navigate to download-book-{id}.html first.
    """
    # # Check for direct file host link
    links = await page.query_selector_all('a[href*="ctfile.com"]')
    for link in links:
        href = await link.get_attribute('href')
        if href and '/f/' in href:
            return href, DEFAULT_PASSWORD

    # Check for download-book-{id}.html link
    download_links = await page.query_selector_all(f'a[href*="download-book-{book_id}"], a[href*="download"]')
    for link in download_links:
        href = await link.get_attribute('href')
        if href:
            full_url = urljoin(SOURCE_A_BASE_URL, href)
            print(f"    Navigating to download page: {href}")
            await page.goto(full_url, wait_until='domcontentloaded', timeout=20000)
            await asyncio.sleep(2)

            # # Extract file host link from download page
            ctfile_links = await page.query_selector_all('a[href*="ctfile.com"]')
            for ct_link in ctfile_links:
                ct_href = await ct_link.get_attribute('href')
                if ct_href and '/f/' in ct_href:
                    return ct_href, DEFAULT_PASSWORD

            # Try to find in page text
            body = await page.query_selector('body')
            if body:
                text = await body.inner_text()
                # # Look for file host URL pattern
                url_match = re.search(r'https://url\d+\.ctfile\.com/f/\d+-\d+-\d+[^\s]*', text)
                if url_match:
                    return url_match.group(0), DEFAULT_PASSWORD

    return "", DEFAULT_PASSWORD


async def decrypt_file_host(file_url: str, password: str = DEFAULT_PASSWORD, headless: bool = True) -> Optional[dict]:
    """
    Decrypt file host link and get download API variables.

    Steps:
    1. Navigate to file host URL
    2. Input password
    3. Click 解密文件 button
    4. Wait for page to load
    5. Extract API variables

    Returns dict with api variables or None on failure.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()

        print(f"Decrypting file host link...")
        await page.goto(file_url, wait_until='domcontentloaded', timeout=30000)
        await asyncio.sleep(3)

        # Check if password page
        body_text = await (await page.query_selector('body')).inner_text()

        if '输入文件' in body_text or '访问密码' in body_text or '解密' in body_text:
            # Type password
            input_box = await page.query_selector('input[type="text"], input[type="password"], input.layui-input')
            if input_box and password:
                await input_box.type(password, delay=50)

                # Click decrypt button
                btn = await page.query_selector('button:has-text("解密"), input[type="submit"][value*="解密"], .layui-btn')
                if not btn:
                    btn = await page.query_selector('button, input[type="submit"]')
                if btn:
                    await btn.click()
                    print(f"  Entered password: {password}")
                    await asyncio.sleep(5)

        # Wait for the countdown (60 seconds required by file host)
        print(f"  Waiting {WAIT_SECONDS} seconds for countdown...")

        # Try to detect countdown timer
        try:
            countdown = await page.query_selector('#down_interval, .countdown, #wait_time')
            if countdown:
                remaining = WAIT_SECONDS
                while remaining > 0:
                    try:
                        text = await countdown.inner_text()
                        nums = re.findall(r'\d+', text)
                        if nums:
                            remaining = int(nums[0])
                    except:
                        pass
                    print(f"    Countdown: {remaining}s remaining...", end='\r')
                    await asyncio.sleep(5)
                    remaining -= 5
        except:
            # Fallback: just wait the full time
            for i in range(WAIT_SECONDS // 10):
                print(f"    Waiting... {WAIT_SECONDS - i * 10}s remaining", end='\r')
                await asyncio.sleep(10)

        print()  # New line after countdown

        # Get API variables
        try:
            result = await page.evaluate("""() => {
                return JSON.stringify({
                    api_server: typeof api_server !== 'undefined' ? api_server : null,
                    userid: typeof userid !== 'undefined' ? userid : null,
                    file_id: typeof file_id !== 'undefined' ? file_id : null,
                    share_id: typeof share_id !== 'undefined' ? share_id : '',
                    file_chk: typeof file_chk !== 'undefined' ? file_chk : null,
                    start_time: typeof start_time !== 'undefined' ? start_time : null,
                    wait_seconds: typeof wait_seconds !== 'undefined' ? wait_seconds : 0,
                    verifycode: typeof verifycode !== 'undefined' ? verifycode : null,
                    filename: typeof filename !== 'undefined' ? filename : null,
                    file_size: typeof file_size !== 'undefined' ? file_size : null
                });
            }""")
            data = json.loads(result)

            if data.get('api_server') and data.get('file_id'):
                print(f"  Got API variables (file_id: {data['file_id']})")
                return data
            else:
                print("  Could not extract API variables")
                return None
        except Exception as e:
            print(f"  Error extracting variables: {e}")
            return None
        finally:
            await browser.close()


async def get_download_url(api_vars: dict) -> Optional[str]:
    """
    Call file host API to get real download URL.

    API endpoint: {api_server}/get_file_url.php
    Parameters: uid, fid, folder_id, share_id, file_chk, start_time, wait_seconds, mb, app, acheck, verifycode, rd
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # # Navigate to file host page to set cookies/context
        ct_url = f"FILE_HOST_BASE_URL + '/f/{api_vars['userid']}-{api_vars['file_id']}"
        await page.goto(ct_url, wait_until='domcontentloaded', timeout=15000)
        await asyncio.sleep(2)

        try:
            js_code = f"""(async () => {{
                try {{
                    var url = '{api_vars['api_server']}/get_file_url.php'
                        + '?uid={api_vars["userid"]}'
                        + '&fid={api_vars["file_id"]}'
                        + '&folder_id=0'
                        + '&share_id={api_vars.get("share_id", "")}'
                        + '&file_chk={api_vars["file_chk"]}'
                        + '&start_time={api_vars["start_time"]}'
                        + '&wait_seconds={api_vars.get("wait_seconds", 0)}'
                        + '&mb=0&app=0&acheck=0'
                        + '&verifycode={api_vars["verifycode"]}'
                        + '&rd=' + Math.random();
                    var headers = typeof getAjaxHeaders === 'function' ? getAjaxHeaders() : {{}};
                    var resp = await fetch(url, {{headers: headers}});
                    var data = await resp.json();
                    return JSON.stringify(data);
                }} catch(e) {{ return JSON.stringify({{error: e.message}}); }}
            }})()"""

            result = await page.evaluate(js_code)
            data = json.loads(result)

            if data.get('code') == 200 and data.get('downurl'):
                file_size = data.get('file_size', 0)
                size_mb = int(file_size) / 1024 / 1024 if file_size else 0
                print(f"  Got download URL ({size_mb:.1f} MB)")
                return data['downurl']
            else:
                print(f"  API error: {data}")
                return None
        except Exception as e:
            print(f"  Error: {e}")
            return None
        finally:
            await browser.close()


def download_with_curl(url: str, output_path: str) -> bool:
    """Download file with curl."""
    cmd = [
        'curl', '-L', '-o', output_path, url,
        '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        '-H', 'Referer: ' + FILE_HOST_BASE_URL + '/',
        '--max-time', str(DOWNLOAD_TIMEOUT),
        '--connect-timeout', '30',
        '--retry', '3'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def extract_zip(zip_path: str, output_dir: str) -> list[str]:
    """
    Extract ZIP file with GBK encoding fix for Chinese filenames.

    Python's zipfile uses CP437 by default, but Chinese filenames are GBK encoded.
    We need to decode from CP437 back to bytes, then decode as GBK.
    """
    extracted_files = []

    with zipfile.ZipFile(zip_path, 'r') as zf:
        for info in zf.infolist():
            # Fix GBK encoding: cp437 -> bytes -> gbk
            try:
                # Try to fix the filename encoding
                fixed_name = info.filename.encode('cp437').decode('gbk')
            except (UnicodeDecodeError, UnicodeEncodeError):
                # If that fails, try other common encodings
                try:
                    fixed_name = info.filename.encode('cp437').decode('utf-8')
                except:
                    fixed_name = info.filename  # Keep original if all else fails

            # Sanitize the filename
            fixed_name = sanitize_filename(fixed_name)

            # Update the filename in the info object
            info.filename = fixed_name

            # Extract
            zf.extract(info, output_dir)
            extracted_path = os.path.join(output_dir, fixed_name)
            extracted_files.append(extracted_path)
            print(f"  Extracted: {fixed_name}")

    return extracted_files


def verify_file(filepath: str) -> bool:
    """Verify downloaded file is valid."""
    if not os.path.exists(filepath):
        return False
    size = os.path.getsize(filepath)
    if size < 1024:  # Too small, probably an error page
        return False

    result = subprocess.run(['file', filepath], capture_output=True, text=True)
    output = result.stdout.lower()

    # Check for valid document types
    valid_types = ['pdf document', 'epub', 'mobi', 'zip archive', 'azw']
    return any(t in output for t in valid_types) or size > 1024 * 1024  # >1MB is likely valid


async def download_book(
    title: str,
    author: str = "",
    output_dir: str = ".",
    ctfile_url: str = "",
    password: str = DEFAULT_PASSWORD,
    headless: bool = True
) -> dict:
    """
    Download a single book.

    Args:
        title: Book title
        author: Book author (optional)
        output_dir: Output directory
        ctfile_url: Direct file host URL (optional, skip search if provided)
        password: file host password
        headless: Run browser in headless mode

    Returns:
        {"status": "done"|"failed", "error": "...", "files": [...]}
    """
    clean_title = sanitize_filename(f"{title} - {author}" if author else title)

    # Step 1: Search or use provided URL
    print(f"\n{'='*60}")
    print(f"Book: {title}" + (f" - {author}" if author else ""))
    print(f"{'='*60}")

    if not ctfile_url:
        results = await search_primary_source(title, author, headless)
        if not results:
            return {"status": "failed", "error": "No results found"}
        best = results[0]
        ctfile_url = best['ctfile_url']
        password = best.get('pwd', DEFAULT_PASSWORD)

    # Step 2: Decrypt
    api_vars = await decrypt_file_host(ctfile_url, password, headless)
    if not api_vars:
        return {"status": "failed", "error": "Could not decrypt file host"}

    # Step 3: Get download URL
    downurl = await get_download_url(api_vars)
    if not downurl:
        return {"status": "failed", "error": "Could not get download URL"}

    # Step 4: Download
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, f"{clean_title}.zip")

        print(f"Downloading...")
        success = download_with_curl(downurl, zip_path)
        if not success:
            return {"status": "failed", "error": "curl download failed"}

        # Step 5: Verify and extract
        if not verify_file(zip_path):
            return {"status": "failed", "error": "Downloaded file is invalid"}

        # Check if it's a ZIP file
        result = subprocess.run(['file', zip_path], capture_output=True, text=True)
        if 'zip archive' in result.stdout.lower():
            # Extract ZIP
            print(f"Extracting ZIP...")
            extracted_files = extract_zip(zip_path, output_dir)

            # Filter to ebook formats only
            ebook_files = [f for f in extracted_files
                          if f.lower().endswith(('.pdf', '.epub', '.mobi', '.azw', '.azw3'))]

            if ebook_files:
                total_size = sum(os.path.getsize(f) for f in ebook_files) / 1024 / 1024
                print(f"Done: {len(ebook_files)} file(s), {total_size:.1f} MB")
                for f in ebook_files:
                    print(f"  {os.path.basename(f)}")
                return {"status": "done", "files": ebook_files}
            else:
                return {"status": "done", "files": extracted_files, "warning": "No ebook files found in archive"}
        else:
            # Direct file (not ZIP)
            final_path = os.path.join(output_dir, f"{clean_title}.pdf")
            shutil.move(zip_path, final_path)
            size_mb = os.path.getsize(final_path) / 1024 / 1024
            print(f"Done: {final_path} ({size_mb:.1f} MB)")
            return {"status": "done", "files": [final_path]}


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Download Chinese ebooks from online sources")
    parser.add_argument('--title', required=True, help='Book title')
    parser.add_argument('--author', default='', help='Book author')
    parser.add_argument('--output-dir', default='.', help='Output directory')
    parser.add_argument('--ctfile-url', default='', help='Direct file host URL (skip search)')
    parser.add_argument('--password', default=DEFAULT_PASSWORD, help='File host password')
    parser.add_argument('--no-headless', action='store_true', help='Show browser window')
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    result = await download_book(
        title=args.title,
        author=args.author,
        output_dir=args.output_dir,
        ctfile_url=args.ctfile_url,
        password=args.password,
        headless=not args.no_headless
    )

    if result['status'] == 'done':
        print(f"\nSuccess!")
    else:
        print(f"\nFailed: {result.get('error', 'Unknown error')}")
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
