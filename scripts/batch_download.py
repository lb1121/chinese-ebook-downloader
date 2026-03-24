#!/usr/bin/env python3
"""
Batch download Chinese ebooks from online sources via file hosting service.
Usage: python batch_download.py --book-list books.json --output-dir ~/Books/
Supports resume via _progress.json.
"""
import sys
import os
import json
import asyncio
import time
import re
import subprocess
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))
from search_secondary_source import search_secondary_source, decrypt_ctfile, get_download_url


def sanitize_filename(name: str) -> str:
    """Clean filename for filesystem."""
    name = re.sub(r'[\[\]【】（）()《》]', '', name)
    name = re.sub(r'[：:]', ' -', name)
    name = re.sub(r'[\\/:*?"<>|]', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:200]


def load_progress(output_dir: str) -> dict:
    """Load progress file."""
    progress_file = os.path.join(output_dir, '_progress.json')
    if os.path.exists(progress_file):
        with open(progress_file, 'r') as f:
            return json.load(f)
    return {}


def save_progress(output_dir: str, progress: dict):
    """Save progress file."""
    progress_file = os.path.join(output_dir, '_progress.json')
    with open(progress_file, 'w') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def download_with_curl(url: str, output_path: str) -> bool:
    """Download file with curl."""
    cmd = [
        'curl', '-L', '-o', output_path, url,
        '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        '-H', 'Referer: https://placeholder.example.com/',
        '--max-time', '600',
        '--connect-timeout', '30'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


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
    valid_types = ['pdf document', 'epub', 'mobi', 'zip archive']
    return any(t in output for t in valid_types) or size > 1024 * 1024  # >1MB is likely valid


async def download_book(title: str, author: str, output_dir: str) -> dict:
    """Download a single book. Returns {"status": "done"|"failed", "error": "..."}"""
    clean_title = sanitize_filename(f"{title} - {author}")
    output_path = os.path.join(output_dir, f"{clean_title}.pdf")
    
    # Check if already downloaded
    if os.path.exists(output_path) and verify_file(output_path):
        return {"status": "done", "message": "Already exists"}
    
    # Step 1: Search
    print(f"\n{'='*60}")
    print(f"📚 {title} - {author}")
    print(f"{'='*60}")
    
    results = await search_secondary_source(title, author)
    if not results:
        return {"status": "failed", "error": "No results found"}
    
    best = results[0]
    ctfile_url = best['ctfile_url']
    password = best['password']
    
    # Step 2: Decrypt
    api_vars = await decrypt_ctfile(ctfile_url, password)
    if not api_vars:
        return {"status": "failed", "error": "Could not decrypt download link"}
    
    # Step 3: Get download URL
    downurl = await get_download_url(api_vars)
    if not downurl:
        return {"status": "failed", "error": "Could not get download URL"}
    
    # Step 4: Download
    print(f"⬇️  Downloading...")
    success = download_with_curl(downurl, output_path)
    if not success:
        return {"status": "failed", "error": "curl download failed"}
    
    # Step 5: Verify
    if verify_file(output_path):
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        print(f"✅ Downloaded: {output_path} ({size_mb:.1f} MB)")
        return {"status": "done", "message": f"{size_mb:.1f} MB"}
    else:
        os.remove(output_path) if os.path.exists(output_path) else None
        return {"status": "failed", "error": "File verification failed"}


async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Batch download Chinese ebooks")
    parser.add_argument('--book-list', required=True, help='JSON file with book list')
    parser.add_argument('--output-dir', required=True, help='Output directory')
    parser.add_argument('--start', type=int, default=0, help='Start index (0-based)')
    parser.add_argument('--limit', type=int, default=0, help='Max books to download (0=all)')
    args = parser.parse_args()
    
    # Load book list
    with open(args.book_list, 'r') as f:
        books = json.load(f)
    
    os.makedirs(args.output_dir, exist_ok=True)
    progress = load_progress(args.output_dir)
    
    # Apply start/limit
    books = books[args.start:]
    if args.limit > 0:
        books = books[:args.limit]
    
    stats = {"done": 0, "failed": 0, "skipped": 0}
    
    for i, book in enumerate(books):
        title = book.get('title', '')
        author = book.get('author', '')
        key = f"{title}|{author}"
        
        # Skip already done
        if progress.get(key) == 'done':
            print(f"⏭️  Skipping (already done): {title}")
            stats['skipped'] += 1
            continue
        
        result = await download_book(title, author, args.output_dir)
        
        # Save progress
        progress[key] = result['status']
        save_progress(args.output_dir, progress)
        
        if result['status'] == 'done':
            stats['done'] += 1
        else:
            stats['failed'] += 1
            print(f"❌ Failed: {result.get('error', 'Unknown')}")
        
        # Rate limiting - wait between books
        if i < len(books) - 1:
            wait = 3
            print(f"⏳ Waiting {wait}s before next book...")
            await asyncio.sleep(wait)
        
        # Report every 5 books
        if (i + 1) % 5 == 0:
            print(f"\n📊 Progress: {stats['done']} done, {stats['failed']} failed, {stats['skipped']} skipped ({i+1}/{len(books)})")
    
    # Final summary
    print(f"\n{'='*60}")
    print(f"📊 FINAL SUMMARY")
    print(f"  ✅ Downloaded: {stats['done']}")
    print(f"  ❌ Failed:     {stats['failed']}")
    print(f"  ⏭️  Skipped:    {stats['skipped']}")
    print(f"  📁 Output:     {args.output_dir}")
    print(f"{'='*60}")


if __name__ == '__main__':
    asyncio.run(main())
